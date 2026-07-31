"""针对真实 public.html 的回归测试。

    python -m tests.test_real_page <保存的public.html>

固化 2026-07 实测确认的事实，防止后续改动回退：
  - 内嵌 JSON 真实字段名：path / publicationDateTime / totalPages /
    leadAuthor / hasMultipleAuthors / sourceDisplayName
  - 三级策略在同一页面上必须给出一致的 uuid 集合
  - 日期必须等于 URL 路径日期，不能被展示文本的时区偏移带偏
    （关键回归点：页面显示 "11 Jul 2026"，URL 是 /2026/07/10/，应取 07-10）
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.adapters import get_adapter          # noqa: E402
from gsr.config import load_config            # noqa: E402
from gsr.parsing import mine_embedded_json    # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


# 已知锚点：这篇研报页面显示 "11 Jul 2026 | 12:00am"（UTC+8 渲染），
# 但 URL 是 /2026/07/10/，publicationDateTime=1783699236000=2026-07-10 16:00 UTC。
ANCHOR_UUID = "ce510cb7-570e-477c-932b-8b706e7188d6"
ANCHOR_DATE = date(2026, 7, 10)
ANCHOR_TITLE_FRAGMENT = "Balancing Innovation and Inflation"


def main(path_str: str) -> int:
    path = Path(path_str)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2
    html = path.read_text(encoding="utf-8", errors="ignore")
    cfg = load_config()
    ad = get_adapter("goldman", cfg, session=None)

    print(f"真实页面: {path.name}  ({len(html):,} 字符)\n")

    print("=== 1. 内嵌 JSON 字段名（实测锁定）===")
    blobs = mine_embedded_json(html, ["distributionHeadline"])
    check("挖出内嵌 JSON 对象", len(blobs) > 0, f"{len(blobs)} 个")
    if blobs:
        b = blobs[0]
        for f in ["distributionHeadline", "path", "publicationDateTime",
                  "totalPages", "leadAuthor", "sourceDisplayName"]:
            check(f"字段 {f} 存在", f in b, f"实际字段: {sorted(b)}")

    print("\n=== 2. 三级策略结果一致性 ===")
    res = {}
    for s in ["embedded_json", "dom_testid", "href_regex"]:
        res[s] = getattr(ad, f"_parse_{s}")(html)
        check(f"{s} 有结果", len(res[s]) > 0, f"{len(res[s])} 条")

    if all(res.values()):
        sets = {s: {m.uuid for m in v} for s, v in res.items()}
        check("三级策略 uuid 集合完全一致",
              sets["embedded_json"] == sets["dom_testid"] == sets["href_regex"],
              f"json={len(sets['embedded_json'])} "
              f"dom={len(sets['dom_testid'])} "
              f"regex={len(sets['href_regex'])}")

    print("\n=== 3. 日期不受时区偏移影响（关键回归点）===")
    for s, metas in res.items():
        if not metas:
            continue
        m = next((x for x in metas if x.uuid == ANCHOR_UUID), None)
        if m is None:
            check(f"{s} 找到锚点研报", False, "未找到")
            continue
        check(f"{s} 锚点日期 = {ANCHOR_DATE}（非展示文本的 07-11）",
              m.pub_date == ANCHOR_DATE, f"得到 {m.pub_date}")

    print("\n=== 4. 全部条目日期必须等于 URL 路径日期 ===")
    for s, metas in res.items():
        if not metas:
            continue
        bad = []
        for m in metas:
            info = ad._parse_report_url(m.url)
            if info and info["date"] and m.pub_date != info["date"]:
                bad.append(f"{m.uuid[:8]}: {m.pub_date} != {info['date']}")
        check(f"{s} 无日期偏移", not bad, "; ".join(bad[:3]))

    print("\n=== 5. 元数据完整性（embedded_json）===")
    js = res.get("embedded_json") or []
    if js:
        m = next((x for x in js if x.uuid == ANCHOR_UUID), None)
        check("标题正确", m is not None and ANCHOR_TITLE_FRAGMENT in m.title,
              m.title[:60] if m else "None")
        if m:
            check("页数已取到", m.page_count == 35, str(m.page_count))
            check("分类已从 sourceDisplayName 拆出",
                  m.category == "Portfolio Strategy", str(m.category))
            check("作者含主笔且带多作者后缀",
                  bool(m.authors) and "Mueller-Glissmann" in m.authors
                  and m.authors.endswith("等"), str(m.authors))
            check("公开研报未被标记为受限", m.restricted is False)
            check("正文链接为 .html", m.url.endswith(".html"), m.url)
            check("PDF 链接为 .pdf", m.pdf_url.endswith(".pdf"), m.pdf_url or "")
        cats = {x.category for x in js if x.category}
        check("分类不含 'Research' 前缀残留",
              "Research" not in cats, str(sorted(cats)))
        check("所有条目都有页数",
              all(x.page_count for x in js),
              f"缺失 {sum(1 for x in js if not x.page_count)} 条")

    print("\n=== 6. dom_testid 也能取到分类和作者 ===")
    dm = res.get("dom_testid") or []
    if dm:
        m = next((x for x in dm if x.uuid == ANCHOR_UUID), None)
        if m:
            check("dom 分类正确", m.category == "Portfolio Strategy",
                  str(m.category))
            check("dom 页数正确", m.page_count == 35, str(m.page_count))
            check("dom 作者非空", bool(m.authors), str(m.authors))

    print("\n=== 7. 日期区间过滤（真实数据）===")
    metas = res.get("embedded_json") or res.get("href_regex") or []
    if metas:
        ytd = [m for m in metas
               if ad._in_range(m.pub_date, date(2026, 1, 1), None)]
        check("--since 2026-01-01 有结果", len(ytd) > 0, f"{len(ytd)} 条")
        check("过滤结果均在 2026 年内",
              all(m.pub_date and m.pub_date >= date(2026, 1, 1) for m in ytd))
        jul = [m for m in metas
               if ad._in_range(m.pub_date, date(2026, 7, 1), date(2026, 7, 31))]
        check("7 月区间只含 7 月条目",
              all(m.pub_date.month == 7 for m in jul if m.pub_date),
              str([str(m.pub_date) for m in jul]))

    print("\n=== 8. 翻页配置就绪 ===")
    pag = ad.sc.get("pagination", {})
    check("模式为 page_param", pag.get("mode") == "page_param",
          str(pag.get("mode")))
    check("search_url 已配置", bool(pag.get("search_url")))
    check("query_template 含 {page} 占位符",
          "{page}" in pag.get("query_template", ""))
    check("已开启日期早停", pag.get("early_stop_on_date") is True)
    check("有 scroll 兜底", pag.get("fallback_mode") == "scroll")
    try:
        u = (f"{pag['search_url']}?"
             f"{pag['query_template'].format(page=3)}")
        check("翻页 URL 可正常拼接", "page=3" in u)
        check("拼接后不残留占位符", "{page}" not in u)
    except Exception as e:  # noqa: BLE001
        check("翻页 URL 可正常拼接", False, str(e))

    print("\n=== 9. 条目数与首屏预期一致 ===")
    check("首屏 10 条（说明必须翻页才能拿更多）",
          len(res.get("href_regex", [])) == 10,
          f"实际 {len(res.get('href_regex', []))} 条")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败清单:")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python -m tests.test_real_page <public.html>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
