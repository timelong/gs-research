"""Markdown 分块。

要点：优先在结构边界切（标题 > 空行段落 > 句子），绝不切在句子中间。
切坏了模型会补全或漏译，译文质量塌得很明显。
"""
from __future__ import annotations

import re


def split_markdown(text: str, max_chars: int = 4000,
                   overlap: int = 200) -> list[str]:
    """把 Markdown 切成不超过 max_chars 的块。

    overlap 是块之间重叠的尾部字符数，只作为上下文提示喂给模型，
    翻译时会明确要求模型不要重复输出重叠部分。
    """
    if len(text) <= max_chars:
        return [text] if text.strip() else []

    # 先按二级以上标题切成大段
    sections = _split_by_headings(text)

    blocks: list[str] = []
    for sec in sections:
        if len(sec) <= max_chars:
            blocks.append(sec)
        else:
            blocks.extend(_split_by_paragraph(sec, max_chars))

    # 合并过小的相邻块，减少请求次数
    merged: list[str] = []
    for b in blocks:
        if merged and len(merged[-1]) + len(b) + 2 <= max_chars:
            merged[-1] = merged[-1] + "\n\n" + b
        else:
            merged.append(b)

    if overlap <= 0:
        return [m for m in merged if m.strip()]

    # 加尾部重叠
    out: list[str] = []
    for i, b in enumerate(merged):
        if i == 0:
            out.append(b)
        else:
            tail = merged[i - 1][-overlap:]
            out.append(f"<<<PREV_CONTEXT>>>\n{tail}\n<<<END_PREV_CONTEXT>>>\n\n{b}")
    return [m for m in out if m.strip()]


def _split_by_headings(text: str) -> list[str]:
    lines = text.split("\n")
    sections: list[list[str]] = [[]]
    for ln in lines:
        if re.match(r"^#{1,3}\s+\S", ln) and sections[-1]:
            sections.append([ln])
        else:
            sections[-1].append(ln)
    return ["\n".join(s).strip() for s in sections if "\n".join(s).strip()]


def _split_by_paragraph(text: str, max_chars: int) -> list[str]:
    paras = re.split(r"\n\s*\n", text)
    out: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > max_chars:
            if cur:
                out.append(cur); cur = ""
            out.extend(_split_by_sentence(p, max_chars))
            continue
        if len(cur) + len(p) + 2 <= max_chars:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def _split_by_sentence(text: str, max_chars: int) -> list[str]:
    # 在句末标点后切，避免切断句子
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    out: list[str] = []
    cur = ""
    for s in parts:
        if len(cur) + len(s) + 1 <= max_chars:
            cur = f"{cur} {s}".strip()
        else:
            if cur:
                out.append(cur)
            # 单句就超长（罕见，通常是表格），硬切
            while len(s) > max_chars:
                out.append(s[:max_chars])
                s = s[max_chars:]
            cur = s
    if cur:
        out.append(cur)
    return out


def strip_prev_context(block: str) -> str:
    """去掉重叠上下文标记，得到该块真正需要翻译的部分。"""
    return re.sub(
        r"<<<PREV_CONTEXT>>>.*?<<<END_PREV_CONTEXT>>>\s*",
        "", block, flags=re.S,
    ).strip()
