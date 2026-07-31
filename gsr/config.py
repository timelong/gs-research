"""配置加载。所有可调参数都在 config/ 下的 YAML 里，代码不硬编码。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _project_root() -> Path:
    # gsr/config.py -> gsr/ -> 项目根
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path

    # ---- 便捷访问 ----
    def get(self, path: str, default: Any = None) -> Any:
        """点号路径取值，如 cfg.get('translate.provider')"""
        cur: Any = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def resolve(self, path_str: str) -> Path:
        """相对路径按项目根解析，绝对路径原样返回。"""
        p = Path(os.path.expanduser(path_str))
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.get("paths.data_dir", "./data"))

    @property
    def browser_profile(self) -> Path:
        return self.resolve(self.get("paths.browser_profile", "./.browser-profile"))

    @property
    def db_path(self) -> Path:
        return self.resolve(self.get("paths.db", "./data/reports.db"))

    def source_config(self, name: str) -> dict[str, Any]:
        f = self.root / "config" / "sources" / f"{name}.yaml"
        if not f.exists():
            raise FileNotFoundError(f"找不到站点配置: {f}")
        with f.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def glossary(self) -> dict[str, str]:
        gf = self.get("translate.glossary_file")
        if not gf:
            return {}
        p = self.resolve(gf)
        if not p.exists():
            return {}
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 去掉以下划线开头的注释键
        return {k: v for k, v in data.items()
                if not k.startswith("_") and isinstance(v, str)}

    def provider_config(self, name: str | None = None) -> dict[str, Any]:
        name = name or self.get("translate.provider", "deepseek")
        providers = self.get("translate.providers", {}) or {}
        if name not in providers:
            raise KeyError(
                f"配置里没有名为 '{name}' 的 translate provider。"
                f"可用: {sorted(providers)}"
            )
        pc = dict(providers[name])
        pc["_name"] = name
        return pc


def load_config(path: str | Path | None = None) -> Config:
    root = _project_root()
    cfg_path = Path(path) if path else (root / "config" / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw=raw, root=root)
