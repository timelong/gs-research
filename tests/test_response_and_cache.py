"""响应解析健壮性 + 分块缓存自检。

    python -m tests.test_response_and_cache

背景（两个真实踩到的问题）：

1. 原来取正文是 data["choices"][0]["message"]["content"] 一把梭。
   服务端返回的结构一旦不符（choices 为 None、限流错误塞在 200 里、
   思考型模型只给 reasoning_content），就抛
   "'NoneType' object is not subscriptable" —— 完全看不出发生了什么。

2. 一篇研报切 25 块，第 6 块失败会导致整篇失败，前 5 块的 token 全作废。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.translate.cache import BlockCache                        # noqa: E402
from gsr.translate.providers import (                             # noqa: E402
    AnthropicProvider, OpenAICompatProvider, ProviderError,
    RetryableProviderError,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


def mk_openai() -> OpenAICompatProvider:
    import os
    os.environ["_T_KEY"] = "k"
    return OpenAICompatProvider({
        "_name": "modelscope", "kind": "openai",
        "base_url": "https://x/v1/chat/completions",
        "model": "Qwen/Qwen3.5-397B-A17B", "api_key_env": "_T_KEY",
        "max_tokens": 8192, "temperature": 0.2,
    })


def mk_anthropic() -> AnthropicProvider:
    import os
    os.environ["_T_KEY"] = "k"
    return AnthropicProvider({
        "_name": "claude", "kind": "anthropic",
        "base_url": "https://x/v1/messages", "model": "claude-sonnet-5",
        "api_key_env": "_T_KEY", "max_tokens": 8192, "temperature": 0.2,
    })


def main() -> int:
    p = mk_openai()

    print("=== 1. 正常响应 ===")
    ok = {"choices": [{"message": {"content": "译文内容"},
                       "finish_reason": "stop"}]}
    check("能取出正文", p._extract(ok) == "译文内容")

    print("\n=== 2. 多模态块格式的 content ===")
    blocks = {"choices": [{"message": {"content": [
        {"type": "text", "text": "前半"}, {"type": "text", "text": "后半"}]}}]}
    check("拼接 content 块", p._extract(blocks) == "前半后半")

    print("\n=== 3. 各种畸形响应都给出可读报错（核心回归点）===")
    bad_cases = [
        ("choices 为 None",            {"choices": None}),
        ("choices 为空列表",           {"choices": []}),
        ("choices[0] 为 None",         {"choices": [None]}),
        ("message 为 None",            {"choices": [{"message": None}]}),
        ("message 不是对象",           {"choices": [{"message": "oops"}]}),
        ("完全没有 choices",           {"id": "x", "object": "chat.completion"}),
        ("响应是列表",                 [1, 2, 3]),
        ("响应是字符串",               "gateway timeout"),
        ("响应是 None",                None),
    ]
    for label, data in bad_cases:
        try:
            p._extract(data)
            check(f"{label} 应报错", False, "没报错")
        except ProviderError as e:
            msg = str(e)
            no_crash = "not subscriptable" not in msg
            has_ctx = "modelscope" in msg
            check(f"{label} -> 可读报错", no_crash and has_ctx, msg[:90])
        except Exception as e:  # noqa: BLE001
            check(f"{label} -> 可读报错", False,
                  f"抛了 {type(e).__name__}: {e}")

    print("\n=== 3b. 瞬时故障 vs 配置问题的分类（决定要不要重试）===")
    # 实测收到的空信封：HTTP 200、choices=null、usage 全 0、object/created 为空
    real_envelope = {
        "id": "chatcmpl-2491e6c4", "object": "", "created": 0,
        "model": "Qwen/Qwen3.5-397B-A17B", "system_fingerprint": "",
        "choices": None,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0},
    }
    try:
        p._extract(real_envelope)
        check("空信封应报错", False)
    except RetryableProviderError as e:
        check("空信封判为【可重试】", True)
        check("报错指出这是服务端空信封、与请求内容无关",
              "空信封" in str(e) and "与请求内容无关" in str(e), str(e)[:100])
    except ProviderError:
        check("空信封判为【可重试】", False, "被判成不可重试")

    # 这些是配置问题，重试只会浪费额度
    for label, data in [
        ("max_tokens 截断",
         {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}),
        ("思考模型只给 reasoning",
         {"choices": [{"message": {"content": "",
                                   "reasoning_content": "…"}}]}),
        ("内容被过滤",
         {"choices": [{"message": {"content": ""},
                       "finish_reason": "content_filter"}]}),
    ]:
        try:
            p._extract(data)
            check(f"{label} 应报错", False)
        except RetryableProviderError:
            check(f"{label} 判为【不可重试】", False, "被判成可重试")
        except ProviderError:
            check(f"{label} 判为【不可重试】", True)

    print("\n=== 3c. complete() 会对瞬时故障退避重试 ===")
    import os as _os
    _os.environ["_T_KEY"] = "k"

    class FlakyEnvelope(OpenAICompatProvider):
        """前两次返回空信封，第三次正常。"""
        def __init__(self, pc):
            super().__init__(pc)
            self.n = 0

        def _request(self, system, user):
            self.n += 1
            if self.n < 3:
                return dict(real_envelope)
            return {"choices": [{"message": {"content": "终于成功"},
                                 "finish_reason": "stop"}]}

    fp = FlakyEnvelope({
        "_name": "modelscope", "kind": "openai", "base_url": "https://x",
        "model": "m", "api_key_env": "_T_KEY", "max_tokens": 100,
        "temperature": 0.2, "response_retries": 4,
    })
    import time as _time
    _orig_sleep = _time.sleep
    _time.sleep = lambda s: None          # 测试里不真等
    try:
        out = fp.complete("s", "u")
        check("瞬时故障重试后成功", out == "终于成功", out)
        check("确实重试了 3 次", fp.n == 3, str(fp.n))
    except Exception as e:  # noqa: BLE001
        check("瞬时故障重试后成功", False, f"{type(e).__name__}: {e}")

    class AlwaysEnvelope(FlakyEnvelope):
        def _request(self, system, user):
            self.n += 1
            return dict(real_envelope)

    ap = AlwaysEnvelope({
        "_name": "modelscope", "kind": "openai", "base_url": "https://x",
        "model": "m", "api_key_env": "_T_KEY", "max_tokens": 100,
        "temperature": 0.2, "response_retries": 3,
    })
    try:
        ap.complete("s", "u")
        check("一直空信封最终应报错", False)
    except ProviderError as e:
        check("一直空信封最终应报错", True)
        check("重试次数用尽", ap.n == 3, str(ap.n))
        check("提示降低并发", "concurrency" in str(e), str(e)[-120:])
    finally:
        _time.sleep = _orig_sleep

    class Permanent(FlakyEnvelope):
        def _request(self, system, user):
            self.n += 1
            return {"choices": [{"message": {"content": ""},
                                 "finish_reason": "length"}]}

    pp = Permanent({
        "_name": "x", "kind": "openai", "base_url": "https://x", "model": "m",
        "api_key_env": "_T_KEY", "max_tokens": 100, "temperature": 0.2,
        "response_retries": 4,
    })
    try:
        pp.complete("s", "u")
        check("配置问题应报错", False)
    except ProviderError:
        check("配置问题不重试（只请求一次，不浪费额度）",
              pp.n == 1, f"请求了 {pp.n} 次")

    print("\n=== 4. HTTP 200 里夹带的错误对象 ===")
    for label, data in [
        ("OpenAI 风格 error",
         {"error": {"message": "Rate limit exceeded", "code": "429"}}),
        ("errors 复数",  {"errors": "quota exhausted"}),
        ("顶层 code/message",
         {"code": "InvalidApiKey", "message": "token 无效"}),
    ]:
        try:
            p._extract(data)
            check(f"{label} 应报错", False)
        except ProviderError as e:
            check(f"{label} 应报错", True)
            check(f"{label} 报错含服务端原文",
                  "Rate limit" in str(e) or "quota" in str(e)
                  or "token 无效" in str(e), str(e)[:80])

    print("\n=== 5. 空正文的诊断提示 ===")
    reasoning = {"choices": [{"message": {
        "content": "", "reasoning_content": "让我想想…"},
        "finish_reason": "stop"}]}
    try:
        p._extract(reasoning)
        check("思考型模型空正文应报错", False)
    except ProviderError as e:
        check("思考型模型空正文应报错", True)
        check("提示指出 reasoning_content 问题",
              "reasoning_content" in str(e), str(e)[:100])
        check("提示给出换模型/调大 max_tokens 建议",
              "max_tokens" in str(e) and "思考" in str(e))

    truncated = {"choices": [{"message": {"content": ""},
                              "finish_reason": "length"}]}
    try:
        p._extract(truncated)
        check("截断应报错", False)
    except ProviderError as e:
        check("截断应报错", True)
        check("提示指出 max_tokens 截断",
              "max_tokens" in str(e) and "8192" in str(e), str(e)[:100])
        check("提示给出减小 chunk_chars 建议", "chunk_chars" in str(e))

    filtered = {"choices": [{"message": {"content": None},
                             "finish_reason": "content_filter"}]}
    try:
        p._extract(filtered)
        check("内容过滤应报错", False)
    except ProviderError as e:
        check("内容过滤应报错", True)
        check("提示指出被过滤", "过滤" in str(e), str(e)[:80])

    print("\n=== 6. 报错里带原始响应，便于诊断 ===")
    try:
        p._extract({"choices": [{"message": {"content": ""}}]})
    except ProviderError as e:
        check("报错含原始响应片段", "原始响应" in str(e))

    print("\n=== 7. Anthropic 协议同样健壮 ===")
    a = mk_anthropic()
    check("正常响应可取出",
          a._extract({"content": [{"type": "text", "text": "hi"}]}) == "hi")
    for label, data in [("content 为 None", {"content": None}),
                        ("content 为空", {"content": []}),
                        ("响应是字符串", "boom")]:
        try:
            a._extract(data)
            check(f"{label} 应报错", False)
        except ProviderError as e:
            check(f"{label} -> 可读报错",
                  "not subscriptable" not in str(e) and "claude" in str(e))
    try:
        a._extract({"content": [{"type": "text", "text": ""}],
                    "stop_reason": "max_tokens"})
    except ProviderError as e:
        check("Anthropic 截断提示 max_tokens", "max_tokens" in str(e))

    print("\n=== 8. 分块缓存基本行为 ===")
    d = Path(tempfile.mkdtemp(prefix="gsr-cache-"))
    cp = d / "out.zh.md.parts.json"
    c = BlockCache(cp, provider="modelscope", model="Qwen/Qwen3.5-397B-A17B")
    check("初始为空", len(c) == 0)
    check("未命中返回 None", c.get("block A") is None)
    c.put("block A", "译文 A")
    check("写入后命中", c.get("block A") == "译文 A")
    check("落盘文件已生成", cp.exists())
    check("命中计数正确", c.hits(["block A", "block B"]) == 1)

    print("\n=== 9. 缓存跨进程可复用（重试续跑）===")
    c2 = BlockCache(cp, provider="modelscope", model="Qwen/Qwen3.5-397B-A17B")
    check("重新加载后仍命中", c2.get("block A") == "译文 A")
    check("只补未译的块", c2.hits(["block A", "block B", "block C"]) == 1)

    print("\n=== 10. 换模型或原文变动会使缓存失效 ===")
    c3 = BlockCache(cp, provider="modelscope", model="另一个模型")
    check("换 model 后不命中（避免拿旧译文糊弄）",
          c3.get("block A") is None)
    c4 = BlockCache(cp, provider="deepseek", model="Qwen/Qwen3.5-397B-A17B")
    check("换 provider 后不命中", c4.get("block A") is None)
    check("原文变动后不命中", c2.get("block A 改了一个字") is None)

    print("\n=== 11. 缓存文件损坏不影响流程 ===")
    cp.write_text("{ 这不是合法 JSON", encoding="utf-8")
    c5 = BlockCache(cp, provider="modelscope", model="Qwen/Qwen3.5-397B-A17B")
    check("坏缓存被当作空缓存", len(c5) == 0)
    c5.put("x", "y")
    check("坏缓存可被覆盖写入", c5.get("x") == "y")

    print("\n=== 12. 成功后清理 ===")
    c5.discard()
    check("缓存文件已删除", not cp.exists())
    c5.discard()
    check("重复清理不报错", True)

    print("\n=== 13. 空缓存必须是 truthy（曾导致缓存完全不生效）===")
    empty = BlockCache(d / "empty.parts.json", provider="p", model="m")
    check("len 为 0", len(empty) == 0)
    check("但 bool 为 True（否则 `if cache:` 会跳过缓存）", bool(empty) is True)

    print("\n=== 14. 落盘内容格式 ===")
    c6 = BlockCache(d / "z.parts.json", provider="p", model="m")
    c6.put("a", "A")
    raw = json.loads((d / "z.parts.json").read_text(encoding="utf-8"))
    check("含 signature 字段", raw.get("signature") == "p/m", str(raw)[:80])
    check("含 blocks 字段", isinstance(raw.get("blocks"), dict))

    print("\n=== 15. Translator 接入缓存 ===")
    from gsr.config import load_config
    from gsr.translate.translator import Translator
    cfg = load_config()
    check("配置项 cache_blocks 默认开启",
          cfg.get("translate.cache_blocks") is True,
          str(cfg.get("translate.cache_blocks")))

    # 用假 provider 验证：第一次全译，第二次全部命中缓存
    calls = {"n": 0}

    class FakeProvider:
        name, model = "fake", "fake-1"

        def complete(self, system, user):
            calls["n"] += 1
            return "译文"

    tr = Translator(cfg, provider=FakeProvider())
    src = d / "a.original.md"
    src.write_text("---\ntitle: x\n---\n" + ("Para one. " * 400
                   + "\n\nPara two. " * 400), encoding="utf-8")
    dest = d / "a.zh.md"

    tr.translate_file(src, dest)
    first = calls["n"]
    check("首次翻译调用了模型", first > 1, str(first))
    check("输出文件已生成", dest.exists())
    check("成功后缓存已清理",
          not (d / "a.zh.md.parts.json").exists())

    # 模拟中途失败：让第 2 块开始报错，验证第 1 块被缓存下来
    calls["n"] = 0
    boom = {"n": 0}

    class FlakyProvider:
        name, model = "fake", "fake-1"

        def complete(self, system, user):
            boom["n"] += 1
            if boom["n"] > 1:
                raise ProviderError("模拟中途失败")
            return "译文"

    tr2 = Translator(cfg, provider=FlakyProvider())
    tr2.concurrency = 1
    dest2 = d / "b.zh.md"
    src2 = d / "b.original.md"
    # 每块内容必须各不相同，否则相同文本会命中同一缓存 key
    # （那是期望行为，但会干扰这里的调用次数断言）
    src2.write_text(
        "\n\n".join(f"## Section {i}\n\n" + f"Unique sentence {i}. " * 400
                    for i in range(4)),
        encoding="utf-8")
    try:
        tr2.translate_file(src2, dest2)
        check("中途失败应抛出", False)
    except Exception:
        check("中途失败应抛出", True)
    cache_file = d / "b.zh.md.parts.json"
    check("失败后缓存文件保留（已译块不作废）", cache_file.exists())
    n_kept = 0
    if cache_file.exists():
        kept = json.loads(cache_file.read_text(encoding="utf-8"))
        n_kept = len(kept.get("blocks") or {})
        check("缓存里确实存下了成功的块", n_kept >= 1, str(n_kept))

    print("\n=== 16. 重试时复用缓存，只补失败的块（省 token）===")
    from gsr.translate.chunker import split_markdown
    _fm, _body = (lambda t: ("", t))(src2.read_text(encoding="utf-8"))
    total_blocks = len(split_markdown(_body, tr2.chunk_chars, tr2.chunk_overlap))
    calls2 = {"n": 0}

    class GoodProvider:
        name, model = "fake", "fake-1"

        def complete(self, system, user):
            calls2["n"] += 1
            return "译文"

    tr3 = Translator(cfg, provider=GoodProvider())
    tr3.concurrency = 1
    tr3.translate_file(src2, dest2)      # 同一个 dest，会读到上次的缓存
    check("重试成功产出文件", dest2.exists())
    check(f"重试只调用了 {total_blocks - n_kept} 次而非 {total_blocks} 次",
          calls2["n"] == total_blocks - n_kept,
          f"实际调用 {calls2['n']} 次，缓存命中 {n_kept} 块")
    check("重试成功后缓存已清理", not cache_file.exists())
    out_text = dest2.read_text(encoding="utf-8")
    check("译文块数完整（缓存块+新译块都在）",
          out_text.count("译文") == total_blocks,
          f"出现 {out_text.count('译文')} 次，期望 {total_blocks}")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败清单:")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
