"""核心数据模型。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


@dataclass
class ReportMeta:
    """一篇研报的元数据。adapter 的 list() 返回这个，后续所有环节都围绕它。"""

    source: str                      # 站点标识，如 "goldman"
    uuid: str                        # 站点内唯一 ID（高盛是 URL 里的 UUID）
    title: str
    url: str                         # 详情页 URL（.html）
    pub_date: Optional[date] = None
    pdf_url: Optional[str] = None
    summary: Optional[str] = None
    category: Optional[str] = None
    authors: Optional[str] = None
    page_count: Optional[int] = None
    # 列表页 JSON 的 restrictionDetails / securityRestrictionMap 非空即为 True，
    # 说明该研报有访问限制，抓取很可能拿不到正文
    restricted: bool = False
    # 记录这条元数据是哪个解析策略产出的，便于排查解析退化
    parsed_by: str = "unknown"

    @property
    def report_id(self) -> str:
        """跨源唯一主键。有 uuid 用 uuid，没有就用 URL 哈希兜底。"""
        if self.uuid:
            return f"{self.source}:{self.uuid}"
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:32]
        return f"{self.source}:{digest}"

    @property
    def safe_title(self) -> str:
        """可用作文件名的标题：去掉非法字符、压缩空白、限长。"""
        t = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", self.title or "untitled")
        t = re.sub(r"\s+", " ", t).strip()
        return t[:120] or "untitled"

    def archive_subdir(self) -> str:
        """归档相对路径：<source>/<YYYY>/<MM>/"""
        if self.pub_date:
            return f"{self.source}/{self.pub_date:%Y}/{self.pub_date:%m}"
        return f"{self.source}/unknown-date"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pub_date"] = self.pub_date.isoformat() if self.pub_date else None
        d["report_id"] = self.report_id
        return d


@dataclass
class FetchResult:
    """一次抓取的产物落盘位置。"""

    meta: ReportMeta
    html_path: Optional[str] = None
    pdf_path: Optional[str] = None
    translated_path: Optional[str] = None
    errors: list[str] = field(default_factory=list)
