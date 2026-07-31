"""针对真实 search.html 的回归测试。

    python -m tests.test_real_search <保存的Search.html>

固化 2026-07 实测确认的事实：
  - search.html 与 public.html 结构完全不同：表格布局，
    class 名是语义化的 SearchResults__xxx（非编译 hash），可安全依赖
  - search_table 策略必须拿到全部元数据（摘要/分类/作者/页数），
    不能退化到只有链接的 href_regex
  - 每页 25 条；结果总数与总页数可从页面读出
  - 日期同样取 URL 路径，不受展示文本的时区偏移影响
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.adapters import get_adapter          # noqa: E402
from gsr.config import load_config            # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


ANCHOR_UUID = "ce510cb7-570e-477c-932b-8b706e7188d6"
ANCHOR_DATE = date(2026, 7, 10)     # 页面展示为 "11 Jul 2026"（UTC+8）
PER_PAGE = 25


def main(path_str: str) -> int:
    path = Path(path_str)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2
    html = path.read_text(encoding="utf-8", errors="ignore")
    cfg = load_config()
    ad = get_adapter("goldman", cfg, session=None)

    print(f"真实检索页: {path.name}  ({len(html):,} 字符)\n")

    print("=== 1. search_table 策略生效 ===")
    ms = ad._parse_search_table(html)
    check("解析出条目", len(ms) > 0, f"{len(ms)} 条")
    check(f"每页 {PER_PAGE} 条", len(ms) == PER_PAGE, f"实际 {len(ms)}")

    print("\n=== 2. 策略优先级：不能退化到 href_regex ===")
    picked = ad._parse_with_strategies(html, quiet=True)
    check("走到有元数据的策略", len(picked) > 0)
    if picked:
        by = {m.parsed_by for m in picked}
        check("使用 search_table 而非 href_regex", by == {"search_table"},
              str(by))
    # public.html 专用的两级在这一页应当空转
    check("embedded_json 在此页无结果（预期）",
          len(ad._parse_embedded_json(html)) == 0)
    check("dom_testid 在此页无结果（预期）",
          len(ad._parse_dom_testid(html)) == 0)
    check("href_regex 仍可兜底", len(ad._parse_href_regex(html)) == PER_PAGE)

    print("\n=== 3. 元数据完整度（每一条都要有）===")
    for f, label in [("pub_date", "日期"), ("title", "标题"),
                     ("url", "正文链接"), ("pdf_url", "PDF链接"),
                     ("summary", "摘要"), ("category", "分类"),
                     ("authors", "作者"), ("page_count", "页数")]:
        n = sum(1 for m in ms if getattr(m, f))
        check(f"{label}全部有值", n == len(ms), f"{n}/{len(ms)}")

    print("\n=== 4. 锚点条目字段正确性 ===")
    m = next((x for x in ms if x.uuid == ANCHOR_UUID), None)
    check("找到锚点条目", m is not None)
    if m:
        check(f"日期为 {ANCHOR_DATE}（非展示文本的 07-11）",
              m.pub_date == ANCHOR_DATE, str(m.pub_date))
        check("标题完整未截断",
              m.title == "Global Strategy Paper: Balancing Innovation "
                         "and Inflation in Portfolios", m.title)
        check("页数 = 35", m.page_count == 35, str(m.page_count))
        check("分类已从 metaText 拆出且无 Research 前缀",
              m.category == "Portfolio Strategy", str(m.category))
        check("作者含主笔", "Mueller-Glissmann" in (m.authors or ""),
              str(m.authors))
        check("作者含多位且带多作者后缀",
              (m.authors or "").endswith("等") and "," in (m.authors or ""),
              str(m.authors))
        check("作者串无重复空格",
              "  " not in (m.authors or ""), repr(m.authors))
        check("作者串不残留 and others",
              "and others" not in (m.authors or ""), str(m.authors))
        check("摘要非空且够长", len(m.summary or "") > 80,
              f"{len(m.summary or '')} 字符")
        check("正文链接为 .html", m.url.endswith(".html"), m.url)
        check("PDF 链接为 .pdf", m.pdf_url.endswith(".pdf"), m.pdf_url)
        check("链接为绝对地址",
              m.url.startswith("https://www.gspublishing.com/"), m.url)

    print("\n=== 5. 日期一律等于 URL 路径日期 ===")
    bad = []
    for x in ms:
        info = ad._parse_report_url(x.url)
        if info and info["date"] and x.pub_date != info["date"]:
            bad.append(f"{x.uuid[:8]}: {x.pub_date} != {info['date']}")
    check("无时区偏移", not bad, "; ".join(bad[:3]))

    print("\n=== 6. 分类值合理性 ===")
    cats = sorted({x.category for x in ms if x.category})
    check("分类不含管道符残留",
          all("|" not in c for c in cats), str(cats))
    check("分类不等于 'Research'", "Research" not in cats, str(cats))
    print(f"    实际分类: {cats}")

    print("\n=== 7. 结果总数与总页数可读出 ===")
    sel = ad.sc.get("search_dom_selectors", {})
    total, pages = ad._parse_result_total(
        html, sel.get("result_summary", ""), sel.get("pager", ""))
    check("读出结果总数", isinstance(total, int) and total > 0, str(total))
    check("读出总页数", isinstance(pages, int) and pages > 0, str(pages))
    if total and pages:
        # 654 条 / 25 条每页 = 27 页（向上取整）
        expect = -(-total // PER_PAGE)
        check("总页数与总条数自洽", pages == expect,
              f"{pages} 页 vs 期望 {expect} 页（{total} 条 / {PER_PAGE}）")
        print(f"    公开研报库共 {total} 条，{pages} 页")

    print("\n=== 8. metaText 拆分的边界情况 ===")
    cases = [
        ("Research | Economics -  Jan Hatzius", "Economics", "Jan Hatzius"),
        ("Research | Commodities -  A,  B, and others…", "Commodities", None),
        ("Research | Economics", "Economics", None),
        ("", None, None),
    ]
    for text, want_cat, want_auth in cases:
        cat, auth = ad._split_meta_text(text)
        ok = cat == want_cat and (want_auth is None or auth == want_auth)
        check(f"拆分 {text[:38]!r}", ok, f"得到 ({cat!r}, {auth!r})")
    _, a = ad._split_meta_text("Research | Commodities -  A,  B, and others…")
    check("多作者场景后缀正确", a == "A, B 等", repr(a))

    print("\n=== 9. 就绪判据同时覆盖两种页面 ===")
    rs = ad.sc.get("ready_selector", "")
    check("含 search.html 的判据", "SearchResults__headline" in rs, rs)
    check("含 public.html 的判据", "query-list-container" in rs, rs)
    from bs4 import BeautifulSoup
    check("判据能在本页命中",
          BeautifulSoup(html, "lxml").select_one(rs) is not None)

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
        print("用法: python -m tests.test_real_search <Search.html>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
