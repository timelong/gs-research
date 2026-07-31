"""分块级翻译缓存。

为什么需要：一篇 35 页研报会切成 25 个分块。如果第 6 块失败，
整篇就失败了，而前 5 块已经花掉的 token 全部作废——重试从头再来。
研报越长越亏。

做法：每块译完立刻落盘到一个 sidecar JSON。重试时命中缓存的块直接复用，
只补没译成的那些。整篇成功后缓存文件自动删掉。

缓存 key = 分块原文 + provider + model 的哈希。
所以换模型或原文变了会自然失效，不会拿旧译文糊弄。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class BlockCache:
    def __init__(self, path: Path, *, provider: str, model: str):
        self.path = Path(path)
        self.sig = f"{provider}/{model}"
        self._data: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("signature") == self.sig:
                self._data = dict(raw.get("blocks") or {})
            # signature 不一致（换了模型）就当缓存不存在，重新翻
        except Exception:  # noqa: BLE001
            self._data = {}

    def _save(self) -> None:
        """原子写：先写临时文件再替换，避免中途被打断留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"signature": self.sig, "blocks": self._data}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    def key(self, block: str) -> str:
        h = hashlib.sha256()
        h.update(self.sig.encode("utf-8"))
        h.update(b"\x00")
        h.update(block.encode("utf-8"))
        return h.hexdigest()[:24]

    def get(self, block: str) -> str | None:
        return self._data.get(self.key(block))

    def put(self, block: str, translated: str) -> None:
        self._data[self.key(block)] = translated
        self._save()

    def hits(self, blocks: list[str]) -> int:
        return sum(1 for b in blocks if self.get(b) is not None)

    def discard(self) -> None:
        """整篇成功后清理。"""
        self.path.unlink(missing_ok=True)

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        """必须显式定义。

        否则因为有 __len__，空缓存会被判定为 falsy，
        `if cache:` 这种写法在缓存为空（也就是首次运行）时直接跳过缓存，
        导致"缓存永远不生效"——这个 bug 曾真实发生过。
        调用方也应统一用 `if cache is not None`。
        """
        return True
