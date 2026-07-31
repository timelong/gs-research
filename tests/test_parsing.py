"""解析器自检。不联网、不需要 Playwright。

    python -m tests.test_parsing      （在项目根目录下运行）

合成的 HTML 按截图里的真实结构复刻：
  - data-testid="query-list-item-container" 包裹每条
  - span id="search-item-title" 内含 <a href="/content/research/en/reports/...">
  - data-testid="search-item-metadata" 存 "10 Jul 2026 | 4:00pm"
  - 另有一份二次转义的内嵌 JSON（distributionHeadline\\":\\"...）
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.adapters import get_adapter          # noqa: E402
from gsr.config import load_config            # noqa: E402
from gsr.daterange import parse_since         # noqa: E402
from gsr.models import ReportMeta             # noqa: E402
from gsr.parsing import (                     # noqa: E402
    html_to_markdown, mine_embedded_json, parse_date_loose, truncate_at_markers,
)
from gsr.storage import Store                 # noqa: E402
from gsr.translate.chunker import split_markdown, strip_prev_context  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "  ok  " if cond else " FAIL "
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail and not cond else ""))


# ----------------------------------------------------------------------
# 合成列表页
# ----------------------------------------------------------------------
ITEMS = [
    ("2026/07/10", "ce510cb7-570e-477c-932b-8b706e7188d6",
     "Global Strategy Paper: Balancing Innovation and Inflation in Portfolios",
     "10 Jul 2026 | 4:00pm", "Portfolio Strategy"),
    ("2026/04/13", "0081122b-77ea-4e46-af63-360457265b8e",
     "The longer-term view", "13 Apr 2026 | 9:30am", "Economics"),
    ("2026/02/08", "531d73dd-fd3f-4647-9841-46e2261e4f4c",
     "Commodity Views: A volatile start to 2026", "8 Feb 2026 | 7:15am",
     "Commodities"),
    ("2025/11/12", "0c292cc7-ce42-4fba-a026-744231e9f4f4",
     "Global Views: Controlled Descent", "12 Nov 2025 | 5:00pm", "Economics"),
]


def _dom_block(datepath, uuid, title, metatext, category) -> str:
    href = f"/content/research/en/reports/{datepath}/{uuid}.html"
    return f'''
<div id="query-list-item-container-{uuid}" data-testid="query-list-item-container"
     class="flex border-b border-neutral-200">
  <div data-testid="query-list-item-content" class="border-b grow">
    <div data-testid="search-item-content" class="pl-2_5 pr-0_75 py-1">
      <div>
        <span data-gs-uitk-component="text" class="gs-uitk-c-hf7351--text-root"
              id="search-item-title" style="text-transform:none">
          <a href="{href}" class="text-text-neutral-bold" target="_self"
             data-impressionid="">{title}</a>
        </span>
      </div>
      <div data-testid="search-item-metadata">
        <span data-gs-uitk-component="text" class="gs-uitk-c-18so4r2--text-root">{metatext}</span>
        <span>{category}</span>
      </div>
    </div>
  </div>
</div>'''


def _embedded_json_script() -> str:
    """模拟二次转义的内嵌 JSON：外层是 JSON 字符串，里层才是真数据。"""
    inner = {
        "results": [
            {
                "distributionHeadline": t,
                "documentUrl": f"/content/research/en/reports/{dp}/{u}.pdf",
                "publishDate": f"{dp.replace('/', '-')}T12:00:00Z",
                "summary": f"Summary for {t}.",
                "researchCategory": cat,
                "authors": [{"name": "Analyst A"}, {"name": "Analyst B"}],
                "pageCount": 24,
            }
            for dp, u, t, _mt, cat in ITEMS
        ]
    }
    # json.dumps 两次 = 外层把里层当字符串塞进去，产生 \" 转义
    escaped = json.dumps(json.dumps(inner, ensure_ascii=False))
    return f'<script>window.__DATA__ = {{"payload": {escaped}}};</script>'


def build_list_html(*, with_json=True, with_dom=True, with_anchors=True) -> str:
    parts = ["<html><head><title>Goldman Sachs Research</title>"]
    if with_json:
        parts.append(_embedded_json_script())
    parts.append('</head><body><div data-testid="query-list-container">')
    if with_dom:
        parts.extend(_dom_block(*it) for it in ITEMS)
    elif with_anchors:
        for dp, u, t, _m, _c in ITEMS:
            parts.append(
                f'<a href="/content/research/en/reports/{dp}/{u}.html">{t}</a>')
    parts.append("</div></body></html>")
    return "\n".join(parts)


DETAIL_HTML = """
<html><body>
<nav>NAV NOISE</nav>
<article>
  <h1>Global Strategy Paper</h1>
  <p>We expect the Fed to cut rates by 75 basis points in 2026, keeping
     real rates modestly restrictive. Our base case assumes core inflation
     settles near 2.4% by year-end.</p>
  <h2>Positioning</h2>
  <ul><li>Overweight equities</li><li>Underweight duration</li></ul>
  <img src="/chart1.png" alt="Exhibit 1: Rate path" />
  <p>Valuation multiples remain elevated relative to history.</p>
  <h2>Disclosure Appendix</h2>
  <p>Reg AC. This legal boilerplate should be truncated away and not translated,
     because it wastes tokens and has no analytical value whatsoever. Repeating
     to make the second half of the document long enough for the marker test.
     More filler text to push the marker past the midpoint of the document.</p>
</article>
<footer>FOOTER NOISE</footer>
</body></html>
"""


# ----------------------------------------------------------------------
def main() -> int:
    cfg = load_config()
    adapter = get_adapter("goldman", cfg, session=None)

    print("\n=== 1. 三级解析策略 ===")
    full = build_list_html()

    j = adapter._parse_embedded_json(full)
    check("embedded_json 解析出全部 4 条", len(j) == 4, f"实际 {len(j)}")
    if j:
        m = next((x for x in j if "Balancing" in x.title), None)
        check("embedded_json 标题正确", m is not None)
        if m:
            check("embedded_json 日期正确", m.pub_date == date(2026, 7, 10),
                  str(m.pub_date))
            check("embedded_json 摘要非空", bool(m.summary))
            check("embedded_json 分类正确", m.category == "Portfolio Strategy",
                  str(m.category))
            check("embedded_json 作者合并为字符串",
                  m.authors == "Analyst A, Analyst B", str(m.authors))
            check("embedded_json 页数正确", m.page_count == 24, str(m.page_count))
            check("PDF 链接以 .pdf 结尾", m.pdf_url.endswith(".pdf"), m.pdf_url or "")
            check("正文链接以 .html 结尾（由 .pdf 推导）",
                  m.url.endswith(".html"), m.url)
            check("链接补全为绝对地址",
                  m.url.startswith("https://www.gspublishing.com/"), m.url)

    d = adapter._parse_dom_testid(full)
    check("dom_testid 解析出全部 4 条", len(d) == 4, f"实际 {len(d)}")
    if d:
        m = next((x for x in d if "Balancing" in x.title), None)
        check("dom_testid 从展示文本解析日期",
              m is not None and m.pub_date == date(2026, 7, 10),
              str(m.pub_date) if m else "None")

    h = adapter._parse_href_regex(full)
    check("href_regex 解析出全部 4 条", len(h) == 4, f"实际 {len(h)}")
    if h:
        m = next((x for x in h if "Balancing" in x.title), None)
        check("href_regex 从 URL 路径取到日期",
              m is not None and m.pub_date == date(2026, 7, 10),
              str(m.pub_date) if m else "None")

    print("\n=== 2. 降级行为 ===")
    no_json = build_list_html(with_json=False)
    check("无内嵌 JSON 时 embedded_json 返回空",
          len(adapter._parse_embedded_json(no_json)) == 0)
    check("无内嵌 JSON 时 dom_testid 仍能解析",
          len(adapter._parse_dom_testid(no_json)) == 4)

    bare = build_list_html(with_json=False, with_dom=False)
    check("DOM 结构全变时 dom_testid 返回空",
          len(adapter._parse_dom_testid(bare)) == 0)
    check("DOM 结构全变时 href_regex 仍能兜底",
          len(adapter._parse_href_regex(bare)) == 4)

    check("完全无关的 HTML 不产生误报",
          len(adapter._parse_href_regex("<html><a href='/about'>x</a></html>")) == 0)

    print("\n=== 3. 日期区间过滤 ===")
    metas = adapter._parse_href_regex(full)
    kept = [m for m in metas if adapter._in_range(m.pub_date, date(2026, 1, 1), None)]
    check("--since 2026-01-01 过滤掉 2025 年那条", len(kept) == 3, f"实际 {len(kept)}")
    kept2 = [m for m in metas
             if adapter._in_range(m.pub_date, date(2026, 3, 1), date(2026, 5, 1))]
    check("区间 2026-03-01~2026-05-01 只留 1 条", len(kept2) == 1, f"实际 {len(kept2)}")
    check("无日期条目不被静默丢弃", adapter._in_range(None, date(2026, 1, 1), None))

    print("\n=== 4. --since 写法 ===")
    check("ytd = 今年 1 月 1 日",
          parse_since("ytd") == date(date.today().year, 1, 1))
    check("2026-03-15 精确解析", parse_since("2026-03-15") == date(2026, 3, 15))
    check("all 返回 None", parse_since("all") is None)
    check("30d 早于今天", parse_since("30d") < date.today())
    check("3m 早于 30d", parse_since("3m") < parse_since("30d"))
    try:
        parse_since("下周三")
        check("非法写法应报错", False)
    except ValueError:
        check("非法写法应报错", True)

    print("\n=== 5. 日期宽松解析 ===")
    for raw, want in [
        ("10 Jul 2026 | 4:00pm", date(2026, 7, 10)),
        ("2026-07-10T16:00:00Z", date(2026, 7, 10)),
        ("Jul 10, 2026", date(2026, 7, 10)),
        ("2026/07/10", date(2026, 7, 10)),
        (1752148800000, date(2025, 7, 10)),
    ]:
        got = parse_date_loose(raw)
        check(f"解析 {raw!r}", got == want, f"得到 {got}")
    check("垃圾输入返回 None", parse_date_loose("nonsense") is None)

    print("\n=== 6. 正文提取与清洗 ===")
    md = adapter.extract_body(DETAIL_HTML)
    check("正文包含核心论述", "75 basis points" in md)
    check("保留标题结构", "# " in md)
    check("保留列表项", "Overweight equities" in md)
    check("图表转成占位标记", "[图表：Exhibit 1: Rate path]" in md, md[:200])
    check("剔除 nav 噪音", "NAV NOISE" not in md)
    check("剔除 footer 噪音", "FOOTER NOISE" not in md)
    check("在免责声明处截断", "legal boilerplate" not in md)

    print("\n=== 7. 分块 ===")
    long_md = "\n\n".join(
        f"## Section {i}\n\n" + ("This is a sentence about markets. " * 40)
        for i in range(12)
    )
    blocks = split_markdown(long_md, max_chars=2000, overlap=150)
    check("长文档被切成多块", len(blocks) > 1, f"{len(blocks)} 块")
    check("每块不超上限（含重叠余量）",
          all(len(b) <= 2000 + 150 + 60 for b in blocks),
          f"最大 {max(len(b) for b in blocks)}")
    check("后续块带上下文标记", "<<<PREV_CONTEXT>>>" in blocks[1])
    check("strip 后拿到纯待译内容",
          "<<<PREV_CONTEXT>>>" not in strip_prev_context(blocks[1]))
    rebuilt = "".join(strip_prev_context(b) for b in blocks)
    check("分块无内容丢失（句子数守恒）",
          rebuilt.count("This is a sentence about markets.")
          == long_md.count("This is a sentence about markets."),
          f"{rebuilt.count('This is a sentence about markets.')} vs "
          f"{long_md.count('This is a sentence about markets.')}")
    short = split_markdown("just one line", max_chars=2000)
    check("短文档不分块", len(short) == 1)

    print("\n=== 8. 存储与去重 ===")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="gsr-selftest-")) / "selftest.db"
    with Store(tmp) as st:
        m0 = metas[0]
        check("首次入库返回 True（新记录）", st.upsert_discovered(m0) is True)
        check("重复入库返回 False（去重命中）",
              st.upsert_discovered(m0) is False)
        for m in metas[1:]:
            st.upsert_discovered(m)
        check("库内共 4 条", len(st.query(limit=99)) == 4)

        partial = ReportMeta(source="goldman", uuid=m0.uuid, title=m0.title,
                             url=m0.url, summary="后补的摘要")
        st.upsert_discovered(partial)
        row = st.query(keyword="Balancing", limit=1)[0]
        check("重复入库会补齐空字段", row["summary"] == "后补的摘要",
              str(row["summary"]))
        check("重复入库不覆盖已有日期",
              row["pub_date"] == "2026-07-10", str(row["pub_date"]))

        st.mark_fetched(m0.report_id, html_path="/tmp/a.original.md")
        check("mark_fetched 后进入待翻译队列",
              any(r["report_id"] == m0.report_id for r in st.pending_translate()))
        st.mark_translated(m0.report_id, "/tmp/a.zh.md")
        check("mark_translated 后离开待翻译队列",
              not any(r["report_id"] == m0.report_id
                      for r in st.pending_translate()))
        check("状态统计正确", st.stats().get("translated") == 1, str(st.stats()))

        st.mark_failed(metas[1].report_id, "boom")
        check("失败记录会被重新排入待抓取",
              any(r["report_id"] == metas[1].report_id
                  for r in st.pending_fetch()))

        rid = st.start_run("selftest")
        st.finish_run(rid, discovered=4, fetched=1)
        check("运行记录可写入", rid > 0)
    tmp.unlink(missing_ok=True)

    print("\n=== 9. 内嵌 JSON 挖掘（转义鲁棒性）===")
    plain = '<script>var x = {"distributionHeadline":"Plain","documentUrl":"/a.pdf"};</script>'
    check("未转义 JSON 可挖出",
          len(mine_embedded_json(plain, ["distributionHeadline"])) >= 1)
    check("二次转义 JSON 可挖出",
          len(mine_embedded_json(_embedded_json_script(),
                                 ["distributionHeadline"])) >= 4)
    check("字段名不存在时返回空",
          len(mine_embedded_json(plain, ["noSuchField"])) == 0)
    check("正文里的花括号不会导致误挖",
          len(mine_embedded_json("<p>set {a, b} and {c}</p>",
                                 ["distributionHeadline"])) == 0)

    print("\n=== 10. 免责声明截断边界 ===")
    t = "Analysis. " * 100 + "Disclosure Appendix" + " legal " * 100
    check("后半部分的标记会截断",
          "legal" not in truncate_at_markers(t, ["Disclosure Appendix"]))
    t2 = "We discuss the Disclosure Appendix later. " + "Real analysis. " * 200
    check("前半部分提及不误截",
          "Real analysis" in truncate_at_markers(t2, ["Disclosure Appendix"]))

    print("\n=== 11. 模块导入完整性 ===")
    import importlib
    for mod in ["gsr.cli", "gsr.browser", "gsr.storage", "gsr.config",
                "gsr.parsing", "gsr.models", "gsr.daterange",
                "gsr.adapters.goldman", "gsr.translate.translator",
                "gsr.translate.providers", "gsr.translate.chunker"]:
        try:
            importlib.import_module(mod)
            check(f"导入 {mod}", True)
        except Exception as e:  # noqa: BLE001
            check(f"导入 {mod}", False, f"{type(e).__name__}: {e}")

    print("\n=== 12. CLI 参数解析 ===")
    from gsr.cli import build_parser
    p = build_parser()
    for argv in [["fetch", "--since", "ytd"], ["run", "--since", "2026-01-01"],
                 ["translate", "--limit", "5", "--provider", "claude"],
                 ["list", "--status", "translated"], ["status"],
                 ["parse-test", "x.html"], ["discover", "--since", "30d"]]:
        try:
            p.parse_args(argv)
            check(f"gsr {' '.join(argv)}", True)
        except SystemExit:
            check(f"gsr {' '.join(argv)}", False)

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
