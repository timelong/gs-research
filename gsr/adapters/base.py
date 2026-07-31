"""站点适配层基类。

新增一个研报源 = 加一个 config/sources/<name>.yaml + 继承 BaseAdapter 实现两个方法。
主流程不需要任何改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Optional

from ..models import FetchResult, ReportMeta


class BaseAdapter(ABC):
    name: str = "base"

    def __init__(self, cfg, source_cfg: dict, session):
        self.cfg = cfg
        self.sc = source_cfg
        self.session = session

    # ------------------------------------------------------------------
    @abstractmethod
    def list_reports(
        self,
        since: Optional[date] = None,
        until: Optional[date] = None,
        max_items: int = 100,
    ) -> list[ReportMeta]:
        """列出日期区间内的研报元数据。"""

    @abstractmethod
    def fetch_report(self, meta: ReportMeta, out_dir: Path) -> FetchResult:
        """下载单篇研报（HTML 正文 + 可选 PDF），落盘并返回路径。"""

    # ------------------------------------------------------------------
    def extract_body(self, html: str) -> str:
        """把详情页 HTML 转成干净的 Markdown 正文，供翻译层使用。"""
        raise NotImplementedError


_REGISTRY: dict[str, type[BaseAdapter]] = {}


def register(cls: type[BaseAdapter]) -> type[BaseAdapter]:
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str, cfg, session) -> BaseAdapter:
    # 触发子类注册
    from . import goldman  # noqa: F401

    if name not in _REGISTRY:
        raise KeyError(f"未注册的站点源 '{name}'，可用: {sorted(_REGISTRY)}")
    return _REGISTRY[name](cfg, cfg.source_config(name), session)
