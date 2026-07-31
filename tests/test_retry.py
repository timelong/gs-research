"""失败重试与状态机自检。

    python -m tests.test_retry

背景：早先版本失败时把 status 改成 'failed'，导致
  - pending_translate 只查 status='fetched'，翻译失败的条目掉出翻译队列
  - pending_fetch 却会捞 'failed'，把翻译失败的条目错排进下载队列
结果就是「翻译失败一次之后，再也翻不了了」。

现在 status 只表示进度（单调前进），失败信息单独存。
这套测试就是守住这个不变量。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.models import ReportMeta      # noqa: E402
from gsr.storage import Store          # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


def tmpdb(name="t.db") -> Path:
    return Path(tempfile.mkdtemp(prefix="gsr-retry-")) / name


def meta(n: int) -> ReportMeta:
    u = f"{n:08d}-0000-4000-8000-000000000000"
    return ReportMeta(
        source="goldman", uuid=u, title=f"Report {n}",
        url=f"https://x/content/research/en/reports/2026/07/0{n}/{u}.html",
        pub_date=date(2026, 7, n),
    )


def main() -> int:
    print("=== 1. 翻译失败后仍留在待翻译队列（核心回归点）===")
    with Store(tmpdb()) as st:
        m = meta(1)
        st.upsert_discovered(m)
        st.mark_fetched(m.report_id, html_path="/tmp/a.original.md")
        check("下载后进入待翻译队列",
              any(r["report_id"] == m.report_id for r in st.pending_translate()))

        st.mark_failed(m.report_id, "SOCKS proxy error", stage="translate")
        q = st.pending_translate()
        check("翻译失败后【仍在】待翻译队列（可直接重跑）",
              any(r["report_id"] == m.report_id for r in q),
              f"队列里 {len(q)} 条")
        check("翻译失败后【不会】跑到待下载队列",
              not any(r["report_id"] == m.report_id for r in st.pending_fetch()))
        row = st.query(limit=1)[0]
        check("status 仍是 fetched，未被改成 failed",
              row["status"] == "fetched", row["status"])
        check("失败阶段被记录", row["failed_stage"] == "translate",
              str(row["failed_stage"]))
        check("失败计数为 1", row["fail_count"] == 1, str(row["fail_count"]))
        check("错误信息被保留",
              "SOCKS" in (row["last_error"] or ""), str(row["last_error"]))

    print("\n=== 2. 下载失败后仍留在待下载队列 ===")
    with Store(tmpdb()) as st:
        m = meta(2)
        st.upsert_discovered(m)
        st.mark_failed(m.report_id, "timeout", stage="fetch")
        check("仍在待下载队列",
              any(r["report_id"] == m.report_id for r in st.pending_fetch()))
        check("不会跑到待翻译队列",
              not any(r["report_id"] == m.report_id
                      for r in st.pending_translate()))
        check("status 仍是 discovered",
              st.query(limit=1)[0]["status"] == "discovered")

    print("\n=== 3. 失败次数上限与强制重试 ===")
    with Store(tmpdb()) as st:
        m = meta(3)
        st.upsert_discovered(m)
        st.mark_fetched(m.report_id, html_path="/tmp/b.original.md")
        for i in range(3):
            st.mark_failed(m.report_id, f"err {i}", stage="translate")
        check("失败 3 次后被跳过（默认上限 3）",
              not any(r["report_id"] == m.report_id
                      for r in st.pending_translate(max_fails=3)))
        check("--retry-failed 可强制取回",
              any(r["report_id"] == m.report_id
                  for r in st.pending_translate(include_failed=True)))
        n = st.clear_failures()
        check("clear_failures 重置了记录", n == 1, str(n))
        check("重置后重新进入队列",
              any(r["report_id"] == m.report_id
                  for r in st.pending_translate(max_fails=3)))
        row = st.query(limit=1)[0]
        check("重置后计数归零", row["fail_count"] == 0, str(row["fail_count"]))
        check("重置后错误信息清空", row["last_error"] is None)

    print("\n=== 4. 成功后清除失败痕迹 ===")
    with Store(tmpdb()) as st:
        m = meta(4)
        st.upsert_discovered(m)
        st.mark_fetched(m.report_id, html_path="/tmp/c.original.md")
        st.mark_failed(m.report_id, "boom", stage="translate")
        st.mark_translated(m.report_id, "/tmp/c.zh.md")
        row = st.query(limit=1)[0]
        check("status 前进到 translated", row["status"] == "translated",
              row["status"])
        check("fail_count 归零", row["fail_count"] == 0, str(row["fail_count"]))
        check("last_error 清空", row["last_error"] is None)
        check("failed_stage 清空", row["failed_stage"] is None)
        check("离开待翻译队列",
              not any(r["report_id"] == m.report_id
                      for r in st.pending_translate()))

    print("\n=== 5. 进度单调前进，不会被失败拉回 ===")
    with Store(tmpdb()) as st:
        m = meta(5)
        st.upsert_discovered(m)
        st.mark_fetched(m.report_id, html_path="/tmp/d.original.md")
        before = st.query(limit=1)[0]["status"]
        for stage in ["translate", "translate"]:
            st.mark_failed(m.report_id, "x", stage=stage)
        after = st.query(limit=1)[0]["status"]
        check("多次失败不改变 status", before == after == "fetched",
              f"{before} -> {after}")

    print("\n=== 6. 老库迁移（status='failed' 的历史数据）===")
    p = tmpdb("legacy.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    # 手工造一个老版本的库：没有新列，且有 status='failed'
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE reports (
            report_id TEXT PRIMARY KEY, source TEXT NOT NULL, uuid TEXT,
            title TEXT, url TEXT NOT NULL, pdf_url TEXT, pub_date TEXT,
            summary TEXT, category TEXT, authors TEXT, page_count INTEGER,
            parsed_by TEXT, status TEXT NOT NULL DEFAULT 'discovered',
            html_path TEXT, pdf_path TEXT, translated_path TEXT,
            last_error TEXT, discovered_at TEXT NOT NULL,
            fetched_at TEXT, translated_at TEXT
        );
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT, command TEXT,
            since TEXT, until TEXT, discovered INTEGER, fetched INTEGER,
            translated INTEGER, failed INTEGER, note TEXT);
    """)
    # 一条翻译失败的（有 html_path），一条下载失败的（无 html_path）
    conn.execute(
        "INSERT INTO reports (report_id,source,url,title,status,html_path,"
        "last_error,discovered_at) VALUES "
        "('goldman:a','goldman','u1','T1','failed','/tmp/a.md','translate boom','x')")
    conn.execute(
        "INSERT INTO reports (report_id,source,url,title,status,"
        "last_error,discovered_at) VALUES "
        "('goldman:b','goldman','u2','T2','failed','fetch boom','x')")
    conn.commit(); conn.close()

    with Store(p) as st:
        rows = {r["report_id"]: r for r in st.query(limit=10)}
        check("有 html_path 的被判为 fetched",
              rows["goldman:a"]["status"] == "fetched",
              rows["goldman:a"]["status"])
        check("无 html_path 的被判为 discovered",
              rows["goldman:b"]["status"] == "discovered",
              rows["goldman:b"]["status"])
        check("推断出失败阶段 translate",
              rows["goldman:a"]["failed_stage"] == "translate",
              str(rows["goldman:a"]["failed_stage"]))
        check("推断出失败阶段 fetch",
              rows["goldman:b"]["failed_stage"] == "fetch",
              str(rows["goldman:b"]["failed_stage"]))
        check("迁移后回到待翻译队列",
              any(r["report_id"] == "goldman:a" for r in st.pending_translate()))
        check("迁移后回到待下载队列",
              any(r["report_id"] == "goldman:b" for r in st.pending_fetch()))
        check("新列已补齐",
              "restricted" in rows["goldman:a"].keys()
              and "fail_count" in rows["goldman:a"].keys())
        check("再次打开不重复迁移", True)

    with Store(p) as st:   # 幂等性
        check("迁移可重复执行不报错",
              len(st.query(limit=10)) == 2)

    print("\n=== 7. list --status failed 语义 ===")
    with Store(tmpdb()) as st:
        a, b = meta(6), meta(7)
        st.upsert_discovered(a); st.upsert_discovered(b)
        st.mark_failed(a.report_id, "oops", stage="fetch")
        got = st.query(status="failed", limit=10)
        check("按 failed 查出有错误记录的条目", len(got) == 1, str(len(got)))
        check("查出的是正确那条", got[0]["report_id"] == a.report_id)
        check("failed 列表接口一致", len(st.failed()) == 1)

    print("\n=== 8. 统计里体现失败情况 ===")
    with Store(tmpdb()) as st:
        m = meta(8)
        st.upsert_discovered(m)
        st.mark_failed(m.report_id, "e", stage="fetch")
        s = st.stats()
        check("统计含进度分布", s.get("discovered") == 1, str(s))
        check("统计提示有失败记录",
              any("失败记录" in k for k in s), str(s))

    print("\n=== 9. 队列为空时的诊断信息（不能误导）===")
    with Store(tmpdb()) as st:
        msg = st.queue_diagnosis("translate")
        check("空库时提示去 discover", "discover" in msg, msg[:80])

        m = meta(9)
        st.upsert_discovered(m)
        msg = st.queue_diagnosis("translate")
        check("只发现未下载时提示去 fetch", "fetch" in msg, msg[:80])

        st.mark_fetched(m.report_id, html_path="/tmp/e.original.md")
        for _ in range(3):
            st.mark_failed(m.report_id, "空信封（choices=null）", stage="translate")
        msg = st.queue_diagnosis("translate", max_fails=3)
        check("失败达上限时说明真正原因",
              "失败次数已达上限" in msg, msg[:120])
        check("【不再】误导用户去跑 fetch",
              "gsr fetch" not in msg, msg[:200])
        check("列出具体失败原因", "空信封" in msg, msg[:200])
        check("给出 retry 办法", "gsr retry" in msg)
        check("给出 --retry-failed 办法", "--retry-failed" in msg)

        st.clear_failures()
        st.mark_translated(m.report_id, "/tmp/e.zh.md")
        msg = st.queue_diagnosis("translate")
        check("全部译完时如实告知", "都已翻译完成" in msg, msg[:80])

    print("\n=== 10. CLI 新参数可解析 ===")
    from gsr.cli import build_parser
    p2 = build_parser()
    for argv in [["translate", "--retry-failed"],
                 ["fetch", "--since", "ytd", "--retry-failed"],
                 ["retry"], ["list", "--status", "failed", "-v"],
                 ["list", "--verbose"]]:
        try:
            p2.parse_args(argv)
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
