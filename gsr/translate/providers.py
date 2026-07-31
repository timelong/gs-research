"""可切换的模型 provider 适配层。

换模型只需改 config.yaml 里的 translate.provider，代码零改动。
DeepSeek / 通义 / 智谱 / OpenAI 都兼容 OpenAI 的 chat/completions 协议，
共用 OpenAICompatProvider；Anthropic 协议不同，单独一个类。

统一用 httpx 直接打 HTTP，不引入各家 SDK —— 少四五个依赖，
且各家 SDK 的重试/超时语义不一致，自己控更省心。
"""
from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx


class ProviderError(RuntimeError):
    """不可重试的错误：配置/额度/内容策略等，重试只会浪费额度。"""


class RetryableProviderError(ProviderError):
    """可重试的错误：服务端瞬时故障。

    典型形态是"空信封"——HTTP 200，但 choices 为 null、usage 全 0、
    object/created 为空。实测魔搭在限流或内部错误时就返回这种：

        {"id":"chatcmpl-…","object":"","created":0,
         "model":"Qwen/…","choices":null,
         "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}

    这种不该让整篇翻译失败，退避重试即可。
    """


class BaseProvider(ABC):
    def __init__(self, pc: dict[str, Any]):
        self.pc = pc
        self.name = pc.get("_name", "unknown")
        self.model = pc["model"]
        self.base_url = pc["base_url"]
        self.max_tokens = int(pc.get("max_tokens", 8192))
        self.temperature = float(pc.get("temperature", 0.2))

        # 代理控制。默认 true = 沿用环境变量里的代理设置（HTTPS_PROXY / ALL_PROXY）。
        # 国内厂商（DeepSeek / 通义 / 智谱 / 魔搭）应设为 false 直连：
        # 走境外代理不仅慢，还常因代理不支持而直接失败。
        self.use_system_proxy = bool(pc.get("use_system_proxy", True))
        # 显式指定代理地址，优先于上面的开关
        self.proxy = pc.get("proxy") or None

        # 响应层重试次数（服务端返回空信封等瞬时故障）
        self.response_retries = int(pc.get("response_retries", 4))
        # 该 provider 的并发覆盖值，None 表示用全局配置
        self.concurrency = pc.get("concurrency")

        env = pc.get("api_key_env", "")
        self.api_key = os.environ.get(env, "").strip()
        if not self.api_key:
            raise ProviderError(
                f"未找到 API key。请设置环境变量 {env}，例如:\n"
                f"  export {env}=your_key_here"
            )

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "timeout": 300.0,
            # trust_env=False 时 httpx 忽略 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
            "trust_env": self.use_system_proxy,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.Client(**kwargs)

    # ------------------------------------------------------------------
    @abstractmethod
    def _request(self, system: str, user: str) -> Any:
        """发一次请求，返回已解析的 JSON。"""

    @abstractmethod
    def _extract(self, data: Any) -> str:
        """从响应里取出正文；瞬时故障应抛 RetryableProviderError。"""

    def complete(self, system: str, user: str) -> str:
        """单轮补全。网络层和响应层都会重试。

        为什么响应层也要重试：服务端可能以 HTTP 200 返回空信封
        （choices=null、usage 全 0），这是限流/内部错误的表现，
        属瞬时故障。早先版本把它当致命错误，导致整篇翻译作废。
        """
        last: Exception | None = None
        for attempt in range(1, self.response_retries + 1):
            data = self._request(system, user)
            try:
                return self._extract(data)
            except RetryableProviderError as e:
                last = e
                if attempt >= self.response_retries:
                    break
                sleep = min(60, 5 * (2 ** (attempt - 1)))
                print(f"    [!] {self.name} 返回空响应（疑似限流），"
                      f"{sleep}s 后重试 {attempt}/{self.response_retries - 1}")
                time.sleep(sleep)
        raise ProviderError(
            f"{last}\n\n已重试 {self.response_retries} 次仍失败。"
            f"若频繁出现，把 translate.concurrency 降到 1，"
            f"或给该 provider 单独配 concurrency: 1。"
        ) from last

    # ------------------------------------------------------------------
    def _post_with_retry(self, url: str, *, headers: dict,
                         json_body: dict, retries: int = 4) -> dict:
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with self._client() as client:
                    r = client.post(url, headers=headers, json=json_body)
                # 限流和服务端错误退避重试
                if r.status_code in (429, 500, 502, 503, 504):
                    raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    # 4xx（除限流）是请求本身的问题，重试无意义
                    raise ProviderError(
                        f"HTTP {r.status_code}: {r.text[:500]}"
                    ) from None
                return r.json()
            except ProviderError as e:
                last = e
                msg = str(e)
                retryable = any(c in msg for c in
                                ("429", "500", "502", "503", "504"))
                if not retryable or attempt == retries:
                    raise
                sleep = min(60, 4 * (2 ** (attempt - 1)))
                print(f"    [!] {self.name} 请求失败（{msg[:80]}），"
                      f"{sleep}s 后重试 {attempt}/{retries}")
                time.sleep(sleep)
            except Exception as e:  # noqa: BLE001
                last = e
                hint = self._network_hint(e)
                if hint:
                    # 环境配置问题，重试没意义，直接抛出并给出解决办法
                    raise ProviderError(hint) from e
                if attempt == retries:
                    raise ProviderError(f"{self.name} 请求异常: {e}") from e
                time.sleep(min(60, 4 * (2 ** (attempt - 1))))
        raise ProviderError(f"{self.name} 重试耗尽") from last

    def _network_hint(self, e: Exception) -> str | None:
        """把常见的网络/代理类报错翻译成可操作的提示。"""
        msg = str(e)

        if "socksio" in msg or "SOCKS proxy" in msg:
            return (
                f"[{self.name}] 环境里配了 SOCKS 代理，但缺 socksio 依赖。\n"
                f"\n两条路，按 API 归属选：\n"
                f"  1) 国内 API（魔搭/DeepSeek/通义/智谱）——不该走代理，"
                f"在 config.yaml 该 provider 下加一行直连：\n"
                f"       use_system_proxy: false\n"
                f"  2) 境外 API（Claude/OpenAI）——确实需要代理，装上支持：\n"
                f"       pip install 'httpx[socks]'\n"
                f"\n也可以临时在当前终端取消代理再跑：\n"
                f"       unset ALL_PROXY all_proxy HTTPS_PROXY https_proxy"
            )

        if isinstance(e, httpx.ConnectError) or "ConnectError" in type(e).__name__:
            proxy_state = "沿用系统代理" if self.use_system_proxy else "直连（已绕过代理）"
            return (
                f"[{self.name}] 连不上 {self.base_url}（当前策略：{proxy_state}）。\n"
                f"  - 若是境外 API，检查代理是否正常，"
                f"并确认该 provider 的 use_system_proxy 为 true\n"
                f"  - 若是国内 API，试着设 use_system_proxy: false 直连"
            )

        return None


def _dump(data: Any, limit: int = 700) -> str:
    """把响应体转成可读片段，方便诊断。"""
    try:
        s = json.dumps(data, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(data)
    return s[:limit] + ("…" if len(s) > limit else "")


def _api_error_in_body(data: Any) -> str | None:
    """有些兼容实现会在 HTTP 200 里塞错误对象（限流、余额不足等）。"""
    if not isinstance(data, dict):
        return None
    for key in ("error", "errors"):
        v = data.get(key)
        if not v:
            continue
        if isinstance(v, dict):
            return str(v.get("message") or v.get("msg") or v)
        return str(v)
    # 阿里/智谱风格：顶层 code + message，且没有 choices
    if "choices" not in data and (data.get("code") or data.get("message")):
        return f"code={data.get('code')} message={data.get('message')}"
    return None


class OpenAICompatProvider(BaseProvider):
    """OpenAI / DeepSeek / 通义 / 智谱 / 魔搭 等兼容协议。"""

    def _request(self, system: str, user: str) -> Any:
        return self._post_with_retry(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json_body={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            },
        )

    def _extract(self, data: Any) -> str:
        """从响应里取出正文。每一层都显式检查，报错时带上原始响应。

        之前这里是 data["choices"][0]["message"]["content"] 一把梭，
        任何一层是 None 就抛 'NoneType' object is not subscriptable，
        完全看不出服务端到底返回了什么。
        """
        err = _api_error_in_body(data)
        if err:
            raise ProviderError(
                f"[{self.name}] 服务端返回错误: {err}\n原始响应: {_dump(data)}")

        if not isinstance(data, dict):
            raise ProviderError(
                f"[{self.name}] 响应不是 JSON 对象: {_dump(data)}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RetryableProviderError(
                f"[{self.name}] 响应里没有可用的 choices"
                f"{self._envelope_note(data)}。\n原始响应: {_dump(data)}")

        ch = choices[0] or {}
        if not isinstance(ch, dict):
            raise RetryableProviderError(
                f"[{self.name}] choices[0] 结构异常: {_dump(data)}")

        msg = ch.get("message") or ch.get("delta") or {}
        if not isinstance(msg, dict):
            raise RetryableProviderError(
                f"[{self.name}] message 结构异常: {_dump(data)}")

        content = msg.get("content")
        # 多模态块格式：content 是 [{"type":"text","text":"…"}]
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict))

        if isinstance(content, str) and content.strip():
            return content

        # 到这里说明正文是空的。区分"配置问题"和"瞬时故障"：
        # 前者重试无意义（截断/思考模型/内容过滤），后者该退避重试。
        finish = ch.get("finish_reason") or ch.get("native_finish_reason")
        hint = self._empty_content_hint(ch, msg, data)
        permanent = bool(msg.get("reasoning_content")) or finish in (
            "length", "content_filter", "sensitive")
        raise (ProviderError if permanent else RetryableProviderError)(hint)

    @staticmethod
    def _envelope_note(data: Any) -> str:
        """识别"空信封"特征，用于把话说明白。

        魔搭限流/内部错误时会返回 HTTP 200 但 choices=null、usage 全 0、
        object 为空串、created 为 0。这些特征同时出现基本可以确认是
        服务端瞬时故障，不是我们的请求有问题。
        """
        if not isinstance(data, dict):
            return ""
        usage = data.get("usage") or {}
        zero_usage = (isinstance(usage, dict) and usage
                      and all((usage.get(k) or 0) == 0 for k in
                              ("prompt_tokens", "completion_tokens",
                               "total_tokens")))
        empty_meta = data.get("object") == "" or data.get("created") == 0
        if zero_usage and empty_meta:
            return ("（usage 全为 0 且 object/created 为空 —— "
                    "这是服务端返回的空信封，通常是限流或内部错误，"
                    "与请求内容无关）")
        return ""

    def _empty_content_hint(self, ch: dict, msg: dict, data: Any) -> str:
        finish = ch.get("finish_reason") or ch.get("native_finish_reason")
        lines = [f"[{self.name}] 模型返回的正文为空（finish_reason={finish}）。"]

        if msg.get("reasoning_content"):
            lines += [
                "",
                "响应里只有 reasoning_content（思考内容），没有正文。",
                "这是思考型模型的典型表现。处理办法：",
                "  - 换成非思考型模型，或在 config.yaml 该 provider 下",
                "    把 model 改成不带思考的版本",
                "  - 或调大 max_tokens：思考占掉了额度，正文还没开始写就截断了",
            ]
        elif finish == "length":
            lines += [
                "",
                f"输出因 max_tokens（当前 {self.max_tokens}）截断。处理办法：",
                "  - 调大该 provider 的 max_tokens",
                "  - 或减小 translate.chunk_chars，让每块更短",
            ]
        elif finish in ("content_filter", "sensitive"):
            lines += ["", "内容被安全过滤拦截。可尝试换 provider。"]

        lines += ["", f"原始响应: {_dump(data)}"]
        return "\n".join(lines)


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API。"""

    def _request(self, system: str, user: str) -> Any:
        return self._post_with_retry(
            self.base_url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json_body={
                "model": self.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            },
        )

    def _extract(self, data: Any) -> str:
        err = _api_error_in_body(data)
        if err:
            raise ProviderError(
                f"[{self.name}] 服务端返回错误: {err}\n原始响应: {_dump(data)}")

        if not isinstance(data, dict):
            raise RetryableProviderError(
                f"[{self.name}] 响应不是 JSON 对象: {_dump(data)}")

        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise RetryableProviderError(
                f"[{self.name}] 响应里没有 content 块。\n"
                f"原始响应: {_dump(data)}")

        text = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if text.strip():
            return text

        stop = data.get("stop_reason")
        extra = ""
        if stop == "max_tokens":
            extra = (f"\n输出因 max_tokens（当前 {self.max_tokens}）截断，"
                     f"调大它或减小 translate.chunk_chars。")
        msg = (f"[{self.name}] 模型返回的正文为空（stop_reason={stop}）。{extra}\n"
               f"原始响应: {_dump(data)}")
        raise (ProviderError if stop == "max_tokens"
               else RetryableProviderError)(msg)


_KINDS = {
    "openai": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(pc: dict[str, Any]) -> BaseProvider:
    kind = pc.get("kind", "openai")
    if kind not in _KINDS:
        raise ProviderError(
            f"未知的 provider kind '{kind}'，可用: {sorted(_KINDS)}"
        )
    return _KINDS[kind](pc)
