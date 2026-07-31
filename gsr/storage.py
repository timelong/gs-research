"""SQLite 存储层：元数据、去重、状态跟踪。

状态机（status 只表示"进度走到哪"，是单调前进的）：
    discovered -> fetched -> translated

失败信息单独记，不覆盖 status：
    fail_count / failed_stage / failed_at / last_error

这样设计的原因：早先版本失败时把 status 改成 'failed'，结果
翻译失败的条目掉出了"待翻译"队列（那个队列只查 status='fetched'），
反而被"待下载"队列捞走——失败就再也重试不了正确的那一步。
现在 status 保持在原进度上，失败只是附加信息，重试天然落回正确队列。

fail_count 用于避免无限重试同一个坏条目，超过上限会被跳过，
用 --retry-failed 可强制重试。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import ReportMeta

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_id       TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    uuid            TEXT,
    title           TEXT,
    url             TEXT NOT NULL,
    pdf_url         TEXT,
    pub_date        TEXT,
    summary         TEXT,
    category        TEXT,
    authors         TEXT,
    page_count      INTEGER,
    restricted      INTEGER DEFAULT 0,
    parsed_by       TEXT,

    -- status 只表示进度，单调前进，失败不会把它改掉
    status          TEXT NOT NULL DEFAULT 'discovered',
    html_path       TEXT,
    pdf_path        TEXT,
    translated_path TEXT,

    -- 失败信息与 status 解耦，重试才能落回正确的队列
    last_error      TEXT,
    failed_stage    TEXT,
    failed_at       TEXT,
    fail_count      INTEGER NOT NULL DEFAULT 0,

    discovered_at   TEXT NOT NULL,
    fetched_at      TEXT,
    translated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_reports_status   ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_pubdate  ON reports(pub_date);
CREATE INDEX IF NOT EXISTS idx_reports_source   ON reports(source);

-- 每次运行留一条记录，方便回看抓了什么、错在哪
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    command     TEXT,
    since       TEXT,
    until       TEXT,
    discovered  INTEGER DEFAULT 0,
    fetched     INTEGER DEFAULT 0,
    translated  INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    note        TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        """就地升级老版本的库，不需要用户删库重建。"""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(reports)").fetchall()}

        added = []
        for name, ddl in [
            ("restricted",   "INTEGER DEFAULT 0"),
            ("failed_stage", "TEXT"),
            ("failed_at",    "TEXT"),
            ("fail_count",   "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE reports ADD COLUMN {name} {ddl}")
                added.append(name)

        # 老版本把失败写成 status='failed'，信息已丢失哪一步失败。
        # 按落盘产物反推真实进度，让它们回到正确的队列里。
        rows = self._conn.execute(
            "SELECT COUNT(*) n FROM reports WHERE status = 'failed'"
        ).fetchone()
        legacy = rows["n"] if rows else 0
        if legacy:
            self._conn.execute(
                """
                UPDATE reports SET
                    status = CASE
                        WHEN translated_path IS NOT NULL THEN 'translated'
                        WHEN html_path IS NOT NULL THEN 'fetched'
                        ELSE 'discovered'
                    END,
                    failed_stage = COALESCE(failed_stage, CASE
                        WHEN html_path IS NOT NULL THEN 'translate'
                        ELSE 'fetch'
                    END),
                    fail_count = CASE WHEN fail_count > 0 THEN fail_count ELSE 1 END
                WHERE status = 'failed'
                """
            )
            print(f"[storage] 已迁移 {legacy} 条历史失败记录到正确的进度状态，"
                  f"可以直接重跑")

        if added or legacy:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # 去重 + 入库
    # ------------------------------------------------------------------
    def exists(self, report_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM reports WHERE report_id = ?", (report_id,)
        )
        return cur.fetchone() is not None

    def upsert_discovered(self, meta: ReportMeta) -> bool:
        """登记一条新发现的研报。

        返回 True 表示这是新的（之前没见过），False 表示已存在（去重命中）。
        已存在时只补齐空字段，不覆盖已有值 —— 避免解析退化把好数据冲掉。
        """
        rid = meta.report_id
        if self.exists(rid):
            with self._tx() as c:
                c.execute(
                    """
                    UPDATE reports SET
                        title      = COALESCE(NULLIF(title, ''), ?),
                        pdf_url    = COALESCE(pdf_url, ?),
                        pub_date   = COALESCE(pub_date, ?),
                        summary    = COALESCE(summary, ?),
                        category   = COALESCE(category, ?),
                        authors    = COALESCE(authors, ?),
                        page_count = COALESCE(page_count, ?)
                    WHERE report_id = ?
                    """,
                    (
                        meta.title, meta.pdf_url,
                        meta.pub_date.isoformat() if meta.pub_date else None,
                        meta.summary, meta.category, meta.authors,
                        meta.page_count, rid,
                    ),
                )
            return False

        with self._tx() as c:
            c.execute(
                """
                INSERT INTO reports (
                    report_id, source, uuid, title, url, pdf_url, pub_date,
                    summary, category, authors, page_count, restricted,
                    parsed_by, status, discovered_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'discovered',?)
                """,
                (
                    rid, meta.source, meta.uuid, meta.title, meta.url,
                    meta.pdf_url,
                    meta.pub_date.isoformat() if meta.pub_date else None,
                    meta.summary, meta.category, meta.authors,
                    meta.page_count, int(bool(meta.restricted)),
                    meta.parsed_by, _now(),
                ),
            )
        return True

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def mark_fetched(
        self, report_id: str,
        html_path: str | None = None,
        pdf_path: str | None = None,
    ) -> None:
        """成功即清空失败痕迹（含 fail_count），让它彻底回到正常轨道。"""
        with self._tx() as c:
            c.execute(
                """
                UPDATE reports SET
                    status = 'fetched', html_path = COALESCE(?, html_path),
                    pdf_path = COALESCE(?, pdf_path),
                    fetched_at = ?,
                    last_error = NULL, failed_stage = NULL,
                    failed_at = NULL, fail_count = 0
                WHERE report_id = ?
                """,
                (html_path, pdf_path, _now(), report_id),
            )

    def mark_translated(self, report_id: str, translated_path: str) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE reports SET
                    status = 'translated', translated_path = ?,
                    translated_at = ?,
                    last_error = NULL, failed_stage = NULL,
                    failed_at = NULL, fail_count = 0
                WHERE report_id = ?
                """,
                (translated_path, _now(), report_id),
            )

    def mark_failed(self, report_id: str, error: str,
                    stage: str = "unknown") -> None:
        """记录失败，但**不动 status**。

        status 保持在原进度上，重试时自然落回正确的队列
        （下载失败仍在待下载，翻译失败仍在待翻译）。
        """
        with self._tx() as c:
            c.execute(
                """
                UPDATE reports SET
                    last_error = ?, failed_stage = ?, failed_at = ?,
                    fail_count = fail_count + 1
                WHERE report_id = ?
                """,
                (error[:2000], stage, _now(), report_id),
            )

    def clear_failures(self, report_id: str | None = None) -> int:
        """清空失败计数，让被跳过的条目重新进入队列。"""
        sql = ("UPDATE reports SET fail_count = 0, last_error = NULL, "
               "failed_stage = NULL, failed_at = NULL")
        args: list = []
        if report_id:
            sql += " WHERE report_id = ?"
            args.append(report_id)
        else:
            sql += " WHERE fail_count > 0"
        with self._tx() as c:
            cur = c.execute(sql, args)
        return cur.rowcount

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def pending_fetch(self, limit: int = 100, *,
                      max_fails: int = 3,
                      include_failed: bool = False) -> list[sqlite3.Row]:
        """待下载 = 进度还停在 discovered 的。

        失败过的仍在这个队列里（因为 status 没被改掉），
        只是 fail_count 超过 max_fails 后会被跳过，避免卡在坏条目上。
        include_failed=True 时忽略这个上限（--retry-failed）。
        """
        cap = 10 ** 9 if include_failed else max_fails
        return self._conn.execute(
            """
            SELECT * FROM reports
            WHERE status = 'discovered' AND fail_count < ?
            ORDER BY fail_count ASC, pub_date DESC
            LIMIT ?
            """,
            (cap, limit),
        ).fetchall()

    def pending_translate(self, limit: int = 100, *,
                          max_fails: int = 3,
                          include_failed: bool = False) -> list[sqlite3.Row]:
        """待翻译 = 已下载正文但还没翻译的。翻译失败的仍留在这里可重试。"""
        cap = 10 ** 9 if include_failed else max_fails
        return self._conn.execute(
            """
            SELECT * FROM reports
            WHERE status = 'fetched' AND html_path IS NOT NULL
              AND fail_count < ?
            ORDER BY fail_count ASC, pub_date DESC
            LIMIT ?
            """,
            (cap, limit),
        ).fetchall()

    def queue_diagnosis(self, stage: str, max_fails: int = 3) -> str:
        """队列为空时解释真正的原因。

        早先版本一律提示"先跑 fetch"，但真实原因往往是失败次数已达上限
        被跳过 —— 错误的提示会让人以为数据丢了，白折腾。
        """
        want = "fetched" if stage == "translate" else "discovered"
        row = self._conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = '{want}' THEN 1 ELSE 0 END) AS at_stage,
              SUM(CASE WHEN status = '{want}' AND fail_count >= ?
                       THEN 1 ELSE 0 END) AS skipped,
              COUNT(*) AS total,
              SUM(CASE WHEN status = 'translated' THEN 1 ELSE 0 END) AS done
            FROM reports
            """,
            (max_fails,),
        ).fetchone()

        total = row["total"] or 0
        at_stage = row["at_stage"] or 0
        skipped = row["skipped"] or 0
        done = row["done"] or 0

        if total == 0:
            return ("库里还没有任何研报。先跑：\n"
                    "  python -m gsr discover --since ytd")

        if skipped and skipped >= at_stage:
            errs = self._conn.execute(
                f"""
                SELECT title, fail_count, last_error FROM reports
                WHERE status = '{want}' AND fail_count >= ?
                ORDER BY failed_at DESC LIMIT 3
                """,
                (max_fails,),
            ).fetchall()
            lines = [
                f"有 {skipped} 篇待{'翻译' if stage == 'translate' else '下载'}，"
                f"但失败次数已达上限（{max_fails} 次）被跳过。",
                "",
                "最近的失败原因：",
            ]
            for e in errs:
                lines.append(f"  - {(e['title'] or '')[:46]}（失败 {e['fail_count']} 次）")
                lines.append(f"      {(e['last_error'] or '')[:150]}")
            lines += [
                "",
                "确认原因已解决后，用下面任一方式重试：",
                "  python -m gsr retry                      # 重置失败计数",
                f"  python -m gsr {stage} --retry-failed     # 本次强制重试",
            ]
            return "\n".join(lines)

        if stage == "translate":
            if done == total:
                return f"全部 {total} 篇都已翻译完成。"
            return ("没有已下载正文的研报可翻译。先跑：\n"
                    "  python -m gsr fetch --since ytd")
        return f"没有待下载的研报（库里共 {total} 篇，已下载 {total - at_stage} 篇）。"

    def failed(self, limit: int = 200) -> list[sqlite3.Row]:
        """所有有失败记录的条目（不论进度）。"""
        return self._conn.execute(
            """
            SELECT * FROM reports WHERE last_error IS NOT NULL
            ORDER BY failed_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def query(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[date] = None,
        until: Optional[date] = None,
        keyword: Optional[str] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM reports WHERE 1=1"
        args: list = []
        if source:
            sql += " AND source = ?"; args.append(source)
        if status == "failed":
            # 'failed' 不再是一个 status 值，而是"有失败记录"
            sql += " AND last_error IS NOT NULL"
        elif status:
            sql += " AND status = ?"; args.append(status)
        if since:
            sql += " AND pub_date >= ?"; args.append(since.isoformat())
        if until:
            sql += " AND pub_date <= ?"; args.append(until.isoformat())
        if keyword:
            sql += " AND (title LIKE ? OR summary LIKE ?)"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY pub_date DESC NULLS LAST LIMIT ?"
        args.append(limit)
        return self._conn.execute(sql, args).fetchall()

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) n FROM reports GROUP BY status"
        ).fetchall()
        out = {r["status"]: r["n"] for r in rows}
        extra = self._conn.execute(
            """
            SELECT
              SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS with_error,
              SUM(CASE WHEN fail_count >= 3 THEN 1 ELSE 0 END)        AS stuck
            FROM reports
            """
        ).fetchone()
        if extra and extra["with_error"]:
            out["（其中有失败记录）"] = extra["with_error"]
        if extra and extra["stuck"]:
            out["（失败次数已达上限，需 --retry-failed）"] = extra["stuck"]
        return out

    # ------------------------------------------------------------------
    # 运行记录
    # ------------------------------------------------------------------
    def start_run(self, command: str, since=None, until=None) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, command, since, until) VALUES (?,?,?,?)",
                (_now(), command,
                 since.isoformat() if since else None,
                 until.isoformat() if until else None),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **counts) -> None:
        fields = ["finished_at = ?"]
        args: list = [_now()]
        for k in ("discovered", "fetched", "translated", "failed", "note"):
            if k in counts:
                fields.append(f"{k} = ?")
                args.append(counts[k])
        args.append(run_id)
        with self._tx() as c:
            c.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", args)
