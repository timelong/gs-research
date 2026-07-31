"""正文清理自检。

    python -m tests.test_body_cleanup

素材是从真实 original.md 里一字不改抄下来的噪音（网页阅读器的界面残留），
以及真实正文段落（与 PDF 第一页逐字核对过）。

两条底线：
  1. 所有界面噪音必须清掉
  2. 正文一个字都不能少
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.adapters import get_adapter                              # noqa: E402
from gsr.config import load_config                                # noqa: E402
from gsr.parsing import (                                         # noqa: E402
    ensure_title_heading, strip_reader_chrome,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


# 真实 original.md 第 12~41 行的噪音，原样照抄
REAL_NOISE = """COMMODITY VIEWS

From Energy to Metals: Why to Still Diversify Into Commodities

Table of Contents

×

- [Intro](#chapter_1)
- [Commodities Can Help Diversify Equities/Bonds' Risks Under a Broad Range of Circumstances](#chapter_2)

28 June 2026 | 7:06PM EDT | Research | Commodities | [By Samantha Dart and others](#author-list)

[PDF](/content/research/en/reports/2026/06/28/9f8e4385-0c24-46d9-8610-98fca5140d0a.pdf)

[Share](mailto:?Subject=GS%20Research%20Commodity%20Views%3A%20From%20Energy%20to%20Metals&body=https%3A%2F%2Fpublishing.gs.com%2Fcontent%2F9f8e4385.html)

More

Download Email Summary

Font Size

▼

Listen to report

{}

0:00/12:39
"""

# 真实正文（与 PDF 第一页核对过）
REAL_BODY = """- As energy flows start to normalize through the Strait of Hormuz, we take stock of how commodities have performed year to date, highlighting that the energy supply shock triggered by the Iran conflict is only the most recent of several examples seen over the past year of why strategic investment portfolios can benefit from diversifying into commodities.
- Investing in different commodities can help diversify risks inherent to equity/bond portfolios under different circumstances, including (1) commodity supply shocks, like the Hormuz disruption, which might lead to higher inflation and lower economic growth, (2) structural demand support for commodities that face challenges to growing supply, and (3) a flight to real assets when fiscal sustainability or other financial risks arise.
- In particular, we think that the Iran conflict ultimately reinforces many of the themes supporting power and metals demand, more so than oil and gas.

## Intro

We see many of the drivers behind such circumstances remaining relevant going forward. From a potential increased reliance on EVs, to further investment into renewable power generation, the case for diversification stands.
"""

TITLE = "Commodity Views: From Energy to Metals: Why to Still Diversify Into Commodities"


def main() -> int:
    cfg = load_config()
    ad = get_adapter("goldman", cfg, session=None)
    rc = ad.sc.get("reader_chrome", {})

    print("=== 1. 界面噪音全部清除 ===")
    cleaned = strip_reader_chrome(REAL_NOISE + "\n" + REAL_BODY, rc)

    for label, needle in [
        ("目录标题 Table of Contents", "Table of Contents"),
        ("章节锚点 #chapter_1", "#chapter_1"),
        ("章节锚点 #chapter_2", "#chapter_2"),
        ("PDF 下载按钮", "[PDF]"),
        ("mailto 分享链接", "mailto:"),
        ("More 菜单", "\nMore\n"),
        ("Download Email Summary", "Download Email Summary"),
        ("Font Size", "Font Size"),
        ("Listen to report", "Listen to report"),
        ("空对象字形 {}", "{}"),
        ("音频进度 0:00/12:39", "0:00/12:39"),
        ("关闭图标 ×", "×"),
        ("下拉图标 ▼", "▼"),
        ("报头日期行", "7:06PM EDT"),
        ("作者锚点", "#author-list"),
    ]:
        check(f"清除 {label}", needle not in cleaned,
              f"仍残留: {needle!r}")

    print("\n=== 2. 正文一字不损 ===")
    for label, needle in [
        ("首段核心论述", "Strait of Hormuz"),
        ("第一条要点完整", "benefit from diversifying into commodities"),
        ("第二条要点", "commodity supply shocks, like the Hormuz disruption"),
        ("括号编号未被破坏", "(1) commodity supply shocks"),
        ("第三条要点", "power and metals demand"),
        ("章节标题 Intro", "## Intro"),
        ("正文末段", "the case for diversification stands"),
        ("斜杠短语未受影响", "equity/bond portfolios"),
    ]:
        check(f"保留 {label}", needle in cleaned, f"丢了: {needle!r}")

    body_sentences = [s for s in REAL_BODY.split("\n") if len(s.strip()) > 40]
    kept = sum(1 for s in body_sentences if s.strip() in cleaned)
    check("正文所有长行都在", kept == len(body_sentences),
          f"{kept}/{len(body_sentences)}")

    print("\n=== 3. H1 标题与报头去重 ===")
    with_h1 = ensure_title_heading(cleaned, TITLE)
    check("正文以 H1 开头", with_h1.lstrip().startswith("# "), with_h1[:60])
    check("H1 就是元数据标题", with_h1.split("\n")[0] == f"# {TITLE}",
          with_h1.split("\n")[0][:70])
    check("标题只出现一次（散落的报头碎片已去重）",
          with_h1.count("From Energy to Metals") == 1,
          f"出现 {with_h1.count('From Energy to Metals')} 次")
    check("系列名碎片 COMMODITY VIEWS 已并入标题",
          "COMMODITY VIEWS" not in with_h1)
    check("H1 之后紧接正文",
          with_h1.split("\n")[2].startswith("- As energy flows"),
          with_h1.split("\n")[2][:50])

    plain = ensure_title_heading("Some body text without the title.", TITLE)
    check("正文没标题时会补上", plain.startswith(f"# {TITLE}"))
    check("空标题不改动内容",
          ensure_title_heading("body", "") == "body")
    same = ensure_title_heading(f"{TITLE}\n\nBody here.", TITLE)
    check("标题已存在时就地提升为 H1，不重复添加",
          same.count(TITLE) == 1 and same.startswith("# "), same[:80])
    long_body = ensure_title_heading(
        "# T\n\n" + "\n".join(f"Real content line {i}." for i in range(20)),
        "T")
    check("去重不影响正文行", long_body.count("Real content line") == 20,
          str(long_body.count("Real content line")))

    print("\n=== 4. 不误删正文里形似界面的内容 ===")
    tricky = """We discuss more below.

The PDF version contains additional exhibits.

Table 3 shows the breakdown.

Prices moved 0:30 basis points — not a timestamp.

- Oil demand grew 2.1% in 2026, driven by petrochemicals and aviation fuel.

See [our earlier note](https://www.gspublishing.com/content/research/en/reports/2026/01/05/abc.html) for context.
"""
    t = strip_reader_chrome(tricky, rc)
    check("句中的 more 不被删", "We discuss more below." in t)
    check("句中的 PDF 不被删", "The PDF version contains" in t)
    check("Table 3 不被当成目录删掉", "Table 3 shows" in t)
    check("正文里的普通链接保留",
          "our earlier note" in t, t[:200])
    check("正文数据行保留", "Oil demand grew 2.1%" in t)

    print("\n=== 5. 配置开关 ===")
    off = strip_reader_chrome(REAL_NOISE, {"enabled": False})
    check("enabled=false 时不清理", "Font Size" in off)
    check("空配置时不清理", "Font Size" in strip_reader_chrome(REAL_NOISE, {}))

    print("\n=== 6. 节点级剔除选择器 ===")
    html = """<html><body><article>
      <div class="Toolbar__root"><span>工具栏</span></div>
      <button>Download Email Summary</button>
      <select><option>Font Size</option></select>
      <audio src="x.mp3"></audio>
      <span data-gs-uitk-component="icon" role="img">check</span>
      <span aria-hidden="true">&times;</span>
      <div class="tableOfContents"><a href="#chapter_1">Intro</a></div>
      <div class="ShareMenu"><a href="mailto:?Subject=x">Share</a></div>
      <p>This is the real body paragraph that must survive intact,
         with enough length to be recognised as prose content.</p>
    </article></body></html>"""
    md = ad.extract_body(html, title="T")
    for label, needle in [("工具栏", "工具栏"), ("button 文字", "Download Email"),
                          ("select 选项", "Font Size"),
                          ("图标 check", "check"),
                          ("目录锚点", "#chapter_1"),
                          ("mailto", "mailto:")]:
        check(f"节点级剔除 {label}", needle not in md, md[:200])
    check("正文段落保留", "real body paragraph" in md, md[:200])
    check("补上了 H1", md.lstrip().startswith("#"), md[:40])

    print("\n=== 7. 免责声明截断仍生效 ===")
    long_html = ("<html><body><article><p>"
                 + "Real analysis content. " * 120
                 + "</p><h2>Disclosure Appendix</h2><p>"
                 + "Reg AC legal boilerplate. " * 120
                 + "</p></article></body></html>")
    md2 = ad.extract_body(long_html, title="T")
    check("正文保留", "Real analysis content" in md2)
    check("免责声明被截断", "legal boilerplate" not in md2)

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
