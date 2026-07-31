"""翻译编排：分块 -> 并发调用 provider -> 拼回纯中文 Markdown。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .cache import BlockCache
from .chunker import split_markdown, strip_prev_context
from .providers import BaseProvider, build_provider

SYSTEM_PROMPT = """你是一位资深金融翻译，专精投行卖方研究报告的中译。你的译文会被中文投资研究人员直接阅读和引用。

翻译要求：

1. 译成{target_lang}。译文必须完整覆盖原文全部信息，不得省略、概括或添加。
2. 严格保留原文的 Markdown 结构：标题层级、列表、表格、加粗、代码块、换行,一律照原样保留。
3. 数字、百分比、货币金额、日期、股票代码、公司英文名一律保持原样，不做单位换算。
4. 专有名词处理：机构名、指数名首次出现时用「中文译名（English Original）」的形式，之后只用中文。
5. 术语表中的词条必须按指定译法翻译，不得自行改动。
6. 图表占位标记（如 [图表：...]）原样保留在对应位置。
7. 风格：使用中文金融行业书面语，简洁准确。不要口语化，不要添加语气词，不要用「我们认为」之外的主观修饰。
8. 只输出译文本身。不要输出任何解释、说明、前言、后记，不要用代码块包裹整篇译文。

{glossary_block}"""

USER_PROMPT = """请翻译下面这段研报内容。

{context_note}

===== 原文开始 =====
{content}
===== 原文结束 =====

只输出译文。"""


def _glossary_block(glossary: dict[str, str], content: str) -> str:
    """只注入本块实际出现的术语，避免 prompt 无谓膨胀。"""
    if not glossary:
        return ""
    low = content.lower()
    hits = {k: v for k, v in glossary.items() if k.lower() in low}
    if not hits:
        return ""
    lines = "\n".join(f"- {k} → {v}" for k, v in sorted(hits.items()))
    return f"术语表（必须遵守）：\n{lines}\n"


class Translator:
    def __init__(self, cfg, provider: Optional[BaseProvider] = None,
                 provider_name: Optional[str] = None):
        self.cfg = cfg
        self.provider = provider or build_provider(cfg.provider_config(provider_name))
        self.glossary = cfg.glossary()
        self.target_lang = cfg.get("translate.target_lang", "简体中文")
        self.chunk_chars = int(cfg.get("translate.chunk_chars", 4000))
        self.chunk_overlap = int(cfg.get("translate.chunk_overlap", 200))
        # provider 可以覆盖全局并发：免费额度的服务限流紧，设 1 更稳
        own = getattr(self.provider, "concurrency", None)
        self.concurrency = max(1, int(
            own if own is not None else cfg.get("translate.concurrency", 2)))
        self.use_cache = bool(cfg.get("translate.cache_blocks", True))

    # ------------------------------------------------------------------
    def translate_text(self, text: str, *, label: str = "",
                       cache: "BlockCache | None" = None) -> str:
        blocks = split_markdown(text, self.chunk_chars, self.chunk_overlap)
        if not blocks:
            return ""

        cached = cache.hits(blocks) if cache is not None else 0
        note = f"，已有 {cached} 块可复用（跳过）" if cached else ""
        print(f"  翻译 {label}：{len(blocks)} 个分块，"
              f"provider={self.provider.name}/{self.provider.model}{note}")

        def work(idx_block):
            idx, block = idx_block
            if cache is not None:
                hit = cache.get(block)
                if hit is not None:
                    return idx, hit
            out = self._translate_block(block)
            if cache is not None:
                # 每块译完立刻落盘，后续块失败也不会白烧这块的 token
                cache.put(block, out)
            print(f"    分块 {idx + 1}/{len(blocks)} 完成 "
                  f"({len(block)} -> {len(out)} 字符)")
            return idx, out

        results: list[tuple[int, str]] = []
        if self.concurrency == 1:
            for item in enumerate(blocks):
                results.append(work(item))
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
                results = list(ex.map(work, enumerate(blocks)))

        results.sort(key=lambda t: t[0])
        return _join_blocks([r[1] for r in results])

    def _translate_block(self, block: str) -> str:
        content = strip_prev_context(block)
        has_ctx = content != block.strip()

        system = SYSTEM_PROMPT.format(
            target_lang=self.target_lang,
            glossary_block=_glossary_block(self.glossary, content),
        )
        context_note = (
            "注意：这是长文档的中间片段。上一段的结尾已在原文前作为上下文给出，"
            "请只翻译「原文开始/结束」之间的内容，不要重复翻译上下文部分。"
            if has_ctx else
            "注意：这是文档的一个片段，可能不以完整句子开头或结尾，照实翻译即可。"
        )

        raw = self.provider.complete(
            system,
            USER_PROMPT.format(context_note=context_note, content=content),
        )
        return _clean_output(raw)

    # ------------------------------------------------------------------
    def translate_file(self, src: str | Path, dest: str | Path) -> Path:
        """翻译一个 .original.md 文件，输出纯中文 Markdown。

        front matter（--- 之间的元数据）原样保留，只翻译正文。

        分块级缓存放在 dest 旁边的 .parts.json：中途失败时已译好的块
        会保留，重试只补失败的那些，不重复烧 token。整篇成功后自动删除。
        """
        src, dest = Path(src), Path(dest)
        text = src.read_text(encoding="utf-8")

        cache = None
        if self.use_cache:
            cache = BlockCache(
                dest.parent / (dest.name + ".parts.json"),
                provider=self.provider.name, model=self.provider.model,
            )

        fm, body = _split_front_matter(text)
        translated = self.translate_text(body, label=src.name, cache=cache)

        dest.parent.mkdir(parents=True, exist_ok=True)
        header = fm + "\n" if fm else ""
        note = (
            f"> 本文由机器翻译自高盛研报原文，"
            f"译者：{self.provider.name}/{self.provider.model}。"
            f"关键结论请核对原文 PDF。\n\n"
        )
        dest.write_text(header + note + translated + "\n", encoding="utf-8")
        # 全篇成功，缓存不再需要
        if cache is not None:
            cache.discard()
        return dest


# ----------------------------------------------------------------------
def _split_front_matter(text: str) -> tuple[str, str]:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, flags=re.S)
    if m:
        return m[1], m[2]
    return "", text


def _clean_output(raw: str) -> str:
    """去掉模型可能加上的整篇代码块包裹和常见前言。"""
    s = raw.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", s, flags=re.S)
    if m:
        s = m[1].strip()
    s = re.sub(
        r"^(以下是[^\n]{0,30}译文[^\n]{0,10}[:：]?|译文[:：]|翻译如下[:：]?)\s*\n+",
        "", s,
    )
    return s.strip()


def _join_blocks(blocks: list[str]) -> str:
    text = "\n\n".join(b.strip() for b in blocks if b.strip())
    return re.sub(r"\n{4,}", "\n\n\n", text)
