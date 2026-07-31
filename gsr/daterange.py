"""日期区间解析。支持 ytd / 具体日期 / 相对天数等写法。"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional


def parse_since(value: Optional[str]) -> Optional[date]:
    """把 --since 的值解析成 date。

    支持：
        ytd / this-year        今年 1 月 1 日
        today                  今天
        yesterday              昨天
        7d / 30d / 90d         往前 N 天
        3m / 6m                往前 N 个月（按 30 天近似）
        1y                     往前 1 年
        2026-01-01             具体日期
        2026/01/01             具体日期
        all                    不限（返回 None）
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    today = date.today()

    if s in ("all", "none", ""):
        return None
    if s in ("ytd", "this-year", "thisyear", "今年"):
        return date(today.year, 1, 1)
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)

    m = re.fullmatch(r"(\d+)d", s)
    if m:
        return today - timedelta(days=int(m[1]))
    m = re.fullmatch(r"(\d+)w", s)
    if m:
        return today - timedelta(weeks=int(m[1]))
    m = re.fullmatch(r"(\d+)m", s)
    if m:
        return today - timedelta(days=30 * int(m[1]))
    m = re.fullmatch(r"(\d+)y", s)
    if m:
        return today - timedelta(days=365 * int(m[1]))

    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), 1)
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return date(int(m[1]), 1, 1)

    raise ValueError(
        f"无法解析日期 '{value}'。"
        f"可用写法：ytd / today / 30d / 3m / 1y / 2026-01-01"
    )


def parse_until(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("all", "none", ""):
        return None
    if s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    return parse_since(value)


def describe(since: Optional[date], until: Optional[date]) -> str:
    a = since.isoformat() if since else "不限"
    b = until.isoformat() if until else "今天"
    return f"{a} ~ {b}"
