"""翻译 provider 层自检（不发真实请求）。

    python -m tests.test_providers

重点覆盖代理策略。国内环境常挂 SOCKS/HTTP 代理，而 httpx 默认会自动
接管环境变量里的代理设置，导致：
  - 国内 API（魔搭/DeepSeek/通义/智谱）被绕到境外代理，慢且常直接失败
  - 挂了 SOCKS 但没装 socksio 时，报错信息晦涩且会被无谓重试 3 次
所以每个 provider 都要显式声明 use_system_proxy，并把这类环境问题
转成可操作的提示、且不重试。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.config import load_config                                # noqa: E402
from gsr.translate.providers import (                             # noqa: E402
    AnthropicProvider, OpenAICompatProvider, ProviderError, build_provider,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


# 国内厂商必须直连，境外必须允许走代理
DOMESTIC = {"deepseek", "qwen", "zhipu", "modelscope"}
OVERSEAS = {"claude", "openai"}


def main() -> int:
    cfg = load_config()
    provs = cfg.get("translate.providers", {}) or {}

    # 给所有 provider 塞假 key，才能实例化
    for pc in provs.values():
        os.environ.setdefault(pc["api_key_env"], "test-key-123")

    print("=== 1. provider 全部可实例化 ===")
    built = {}
    for name in provs:
        try:
            built[name] = build_provider(cfg.provider_config(name))
            check(f"{name} 可实例化", True)
        except Exception as e:  # noqa: BLE001
            check(f"{name} 可实例化", False, f"{type(e).__name__}: {e}")

    print("\n=== 2. 协议实现对应正确 ===")
    for name, p in built.items():
        kind = provs[name].get("kind")
        want = AnthropicProvider if kind == "anthropic" else OpenAICompatProvider
        check(f"{name} 用 {want.__name__}", isinstance(p, want),
              type(p).__name__)

    print("\n=== 3. 端点路径后缀 ===")
    for name, p in built.items():
        if provs[name].get("kind") == "anthropic":
            check(f"{name} 端点以 /messages 结尾",
                  p.base_url.endswith("/messages"), p.base_url)
        else:
            check(f"{name} 端点以 /chat/completions 结尾",
                  p.base_url.endswith("/chat/completions"), p.base_url)

    print("\n=== 4. 代理策略（核心回归点）===")
    for name in sorted(DOMESTIC & set(built)):
        check(f"{name}（国内）use_system_proxy=false",
              built[name].use_system_proxy is False,
              str(built[name].use_system_proxy))
    for name in sorted(OVERSEAS & set(built)):
        check(f"{name}（境外）use_system_proxy=true",
              built[name].use_system_proxy is True,
              str(built[name].use_system_proxy))
    missing = [n for n in provs if "use_system_proxy" not in provs[n]]
    check("每个 provider 都显式声明了代理策略", not missing, str(missing))

    print("\n=== 5. trust_env 真的传给了 httpx ===")
    for name in ["modelscope", "claude"]:
        if name not in built:
            continue
        p = built[name]
        if not p.use_system_proxy:
            c = p._client()
            check(f"{name} 客户端 trust_env=False（忽略代理环境变量）",
                  c.trust_env is False, str(c.trust_env))
            c.close()
        else:
            check(f"{name} 允许读取代理环境变量", p.use_system_proxy is True)

    print("\n=== 6. 环境类报错转成可操作提示，且不重试 ===")
    p = built.get("modelscope") or next(iter(built.values()))

    hint = p._network_hint(Exception(
        "Using SOCKS proxy, but the 'socksio' package is not installed."))
    check("SOCKS 缺依赖有提示", bool(hint))
    if hint:
        check("提示里给出 use_system_proxy 方案",
              "use_system_proxy: false" in hint)
        check("提示里给出 httpx[socks] 方案", "httpx[socks]" in hint)
        check("提示里给出 unset 临时方案", "unset" in hint)

    import httpx
    hint2 = p._network_hint(httpx.ConnectError("connection refused"))
    check("连接失败有提示", bool(hint2))
    if hint2:
        check("连接失败提示里带当前代理策略",
              "直连" in hint2 or "系统代理" in hint2, hint2[:80])

    check("普通业务错误不误判为环境问题",
          p._network_hint(ValueError("bad request body")) is None)

    print("\n=== 7. 缺 API key 时提示明确 ===")
    saved = os.environ.pop("MODELSCOPE_API_KEY", None)
    try:
        build_provider(cfg.provider_config("modelscope"))
        check("缺 key 应报错", False, "没报错")
    except ProviderError as e:
        check("缺 key 应报错", True)
        check("报错里指出变量名", "MODELSCOPE_API_KEY" in str(e), str(e)[:80])
    finally:
        if saved:
            os.environ["MODELSCOPE_API_KEY"] = saved

    print("\n=== 8. 未知 provider / 未知 kind ===")
    try:
        cfg.provider_config("nonexistent")
        check("未知 provider 名应报错", False)
    except KeyError:
        check("未知 provider 名应报错", True)
    try:
        build_provider({"kind": "telepathy", "model": "x", "base_url": "y",
                        "api_key_env": "MODELSCOPE_API_KEY", "_name": "t"})
        check("未知 kind 应报错", False)
    except ProviderError as e:
        check("未知 kind 应报错", True)
        check("报错里列出可用 kind", "openai" in str(e), str(e)[:80])

    print("\n=== 9. 温度参数适合翻译任务 ===")
    hot = [(n, p.temperature) for n, p in built.items() if p.temperature > 0.4]
    check("所有 provider 温度 <= 0.4（翻译需稳定输出）", not hot, str(hot))

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
