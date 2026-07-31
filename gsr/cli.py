"""CLI 入口。

    python -m gsr discover --since ytd          只发现列表，入库，不下载
    python -m gsr fetch    --since ytd          发现 + 下载正文和 PDF
    python -m gsr translate --limit 10          翻译已下载但未翻译的
    python -m gsr run      --since ytd          fetch + translate 一条龙
    python -m gsr list     --since 30d          看库里有什么（-v 看错误详情）
    python -m gsr status                        统计
    python -m gsr retry                         看失败记录并重置失败计数
    python -m gsr parse-test <file.html>        用本地 HTML 试解析器，不联网

失败的条目会留在原队列里，下次跑同一命令自动重试；
连续失败超过 fetch.max_fail_retries（默认 3）次后会被跳过，
用 --retry-failed 或 `gsr retry` 可以重新放开。
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .adapters import get_adapter
from .browser import open_session
from .config import load_config
from .daterange import describe, parse_since, parse_until
from .models import ReportMeta
from .storage import Store


# ----------------------------------------------------------------------
def _add_range_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--since", default="ytd",
        help="起始日期。ytd(今年以来,默认) / today / 30d / 3m / 1y / 2026-01-01 / all",
    )
    p.add_argument("--until", default=None, help="截止日期，默认今天")
    p.add_argument("--source", action="append", default=None,
                   help="只处理指定源，可重复。默认用 config.yaml 里 sources 全部")
    p.add_argument("--limit", type=int, default=None,
                   help="本次最多处理多少篇，覆盖配置里的 max_reports_per_run")
    p.add_argument("--retry-failed", action="store_true",
                   help="忽略失败次数上限，强制重试失败过的条目")


def _sources(cfg, args) -> list[str]:
    return args.source or cfg.get("sources", ["goldman"])


# ----------------------------------------------------------------------
def cmd_discover(cfg, args) -> int:
    since, until = parse_since(args.since), parse_until(args.until)
    limit = args.limit or int(cfg.get("fetch.max_reports_per_run", 60))
    print(f"发现模式 | 区间 {describe(since, until)} | 上限 {limit}")

    total_new = 0
    with Store(cfg.db_path) as store, open_session(cfg) as session:
        run_id = store.start_run("discover", since, until)
        for src in _sources(cfg, args):
            try:
                adapter = get_adapter(src, cfg, session)
                metas = adapter.list_reports(since, until, limit)
            except Exception as e:  # noqa: BLE001
                print(f"[{src}] 列表失败: {e}")
                traceback.print_exc()
                continue

            new = 0
            for m in metas:
                if store.upsert_discovered(m):
                    new += 1
                    print(f"  + {m.pub_date} {m.title[:70]}")
            print(f"[{src}] 共 {len(metas)} 条，新增 {new} 条")
            total_new += new
        store.finish_run(run_id, discovered=total_new)

    print(f"\n完成。新增 {total_new} 条。用 `python -m gsr fetch` 下载正文。")
    return 0


def cmd_fetch(cfg, args) -> int:
    since, until = parse_since(args.since), parse_until(args.until)
    limit = args.limit or int(cfg.get("fetch.max_reports_per_run", 60))
    print(f"抓取模式 | 区间 {describe(since, until)} | 上限 {limit}")

    ok = failed = 0
    with Store(cfg.db_path) as store, open_session(cfg) as session:
        run_id = store.start_run("fetch", since, until)

        # 1) 先刷新列表
        for src in _sources(cfg, args):
            try:
                adapter = get_adapter(src, cfg, session)
                metas = adapter.list_reports(since, until, limit)
                new = sum(1 for m in metas if store.upsert_discovered(m))
                print(f"[{src}] 列表 {len(metas)} 条，新增 {new} 条")
            except Exception as e:  # noqa: BLE001
                print(f"[{src}] 列表失败: {e}")

        # 2) 再逐篇下载
        rows = store.pending_fetch(
            limit, max_fails=int(cfg.get("fetch.max_fail_retries", 3)),
            include_failed=bool(getattr(args, "retry_failed", False)))
        if not rows:
            print()
            print(store.queue_diagnosis(
                "fetch", int(cfg.get("fetch.max_fail_retries", 3))))
        print(f"\n待下载 {len(rows)} 篇\n")
        for i, row in enumerate(rows, 1):
            meta = _row_to_meta(row)
            print(f"[{i}/{len(rows)}] {meta.pub_date} {meta.title[:60]}")
            try:
                adapter = get_adapter(meta.source, cfg, session)
                res = adapter.fetch_report(meta, cfg.data_dir)
                if res.errors and not res.html_path:
                    store.mark_failed(meta.report_id, "; ".join(res.errors), stage="fetch")
                    failed += 1
                    print(f"    失败: {'; '.join(res.errors)}")
                else:
                    store.mark_fetched(meta.report_id, res.html_path, res.pdf_path)
                    ok += 1
                    bits = []
                    if res.html_path:
                        bits.append("正文")
                    if res.pdf_path:
                        bits.append("PDF")
                    warn = f"（部分失败: {'; '.join(res.errors)}）" if res.errors else ""
                    print(f"    已保存 {'+'.join(bits)} {warn}")
            except Exception as e:  # noqa: BLE001
                store.mark_failed(meta.report_id, f"{type(e).__name__}: {e}",
                                  stage="fetch")
                failed += 1
                print(f"    异常: {e}")

        store.finish_run(run_id, fetched=ok, failed=failed)

    print(f"\n完成。成功 {ok}，失败 {failed}。")
    if cfg.get("translate.enabled", True):
        print("用 `python -m gsr translate` 翻译。")
    return 0


def cmd_translate(cfg, args) -> int:
    from .translate import Translator

    limit = args.limit or 20
    provider_name = getattr(args, "provider", None)

    with Store(cfg.db_path) as store:
        rows = store.pending_translate(
            limit, max_fails=int(cfg.get("fetch.max_fail_retries", 3)),
            include_failed=bool(getattr(args, "retry_failed", False)))
        if not rows:
            print(store.queue_diagnosis(
                "translate", int(cfg.get("fetch.max_fail_retries", 3))))
            return 0

        print(f"待翻译 {len(rows)} 篇")
        try:
            tr = Translator(cfg, provider_name=provider_name)
        except Exception as e:  # noqa: BLE001
            print(f"翻译器初始化失败: {e}")
            return 1

        ok = failed = 0
        for i, row in enumerate(rows, 1):
            src = Path(row["html_path"])
            if not src.exists():
                store.mark_failed(row["report_id"], f"源文件不存在: {src}",
                                  stage="translate")
                failed += 1
                continue
            dest = src.with_name(src.name.replace(".original.md", "") + ".zh.md")
            print(f"\n[{i}/{len(rows)}] {row['title'][:60]}")
            try:
                tr.translate_file(src, dest)
                store.mark_translated(row["report_id"], str(dest))
                ok += 1
                print(f"    -> {dest.name}")
            except Exception as e:  # noqa: BLE001
                store.mark_failed(row["report_id"], f"翻译失败: {e}", stage="translate")
                failed += 1
                print(f"    失败: {e}")

    print(f"\n完成。成功 {ok}，失败 {failed}。")
    return 0


def cmd_run(cfg, args) -> int:
    rc = cmd_fetch(cfg, args)
    if rc != 0:
        return rc
    if not cfg.get("translate.enabled", True):
        print("配置里 translate.enabled=false，跳过翻译。")
        return 0
    print("\n" + "=" * 60 + "\n开始翻译\n" + "=" * 60)
    args.limit = args.limit or 20
    return cmd_translate(cfg, args)


def cmd_list(cfg, args) -> int:
    since, until = parse_since(args.since), parse_until(args.until)
    with Store(cfg.db_path) as store:
        rows = store.query(
            source=(args.source[0] if args.source else None),
            status=args.status, since=since, until=until,
            keyword=args.keyword, limit=args.limit or 200,
        )
    if not rows:
        print("没有匹配的记录。")
        return 0
    print(f"{'日期':<12} {'进度':<11} {'失败':<6} 标题")
    print("-" * 100)
    for r in rows:
        n = r["fail_count"] or 0
        stage = r["failed_stage"] or ""
        flag = f"{stage[:4]}x{n}" if n else ""
        print(f"{r['pub_date'] or '-':<12} {r['status']:<11} "
              f"{flag:<6} {(r['title'] or '')[:60]}")
        if args.verbose and r["last_error"]:
            print(f"{'':<12} └─ {r['last_error'][:110]}")
    print(f"\n共 {len(rows)} 条")
    stuck = [r for r in rows if (r["fail_count"] or 0) >= 3]
    if stuck:
        print(f"其中 {len(stuck)} 条失败次数已达上限，"
              f"用 `--retry-failed` 或 `python -m gsr retry` 重置后可再试")
    return 0


def cmd_retry(cfg, args) -> int:
    """清空失败计数，让被跳过的条目重新进入队列。"""
    with Store(cfg.db_path) as store:
        rows = store.failed(limit=500)
        if not rows:
            print("没有失败记录。")
            return 0
        print(f"当前有 {len(rows)} 条失败记录：\n")
        for r in rows[:20]:
            print(f"  [{r['failed_stage'] or '?'}] x{r['fail_count']} "
                  f"{r['pub_date'] or '-'} {(r['title'] or '')[:52]}")
            print(f"      {(r['last_error'] or '')[:110]}")
        if len(rows) > 20:
            print(f"  …还有 {len(rows) - 20} 条")

        n = store.clear_failures()
        print(f"\n已重置 {n} 条的失败计数。现在可以重跑："
              f"\n  python -m gsr fetch --since ytd"
              f"\n  python -m gsr translate")
    return 0


def cmd_status(cfg, args) -> int:
    with Store(cfg.db_path) as store:
        stats = store.stats()
    print(f"数据库: {cfg.db_path}")
    print(f"归档目录: {cfg.data_dir}")
    print(f"浏览器 profile: {cfg.browser_profile}")
    print(f"翻译 provider: {cfg.get('translate.provider')}")
    print("\n研报状态统计:")
    if not stats:
        print("  （空）")
    for k, v in sorted(stats.items()):
        print(f"  {k:<12} {v}")
    return 0


def cmd_parse_test(cfg, args) -> int:
    """用本地保存的 HTML 测试解析器，完全不联网。

    调试解析策略最快的方式：浏览器里 Cmd+S 存下 public.html，
    然后 python -m gsr parse-test public.html
    """
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    html = path.read_text(encoding="utf-8", errors="ignore")

    src = args.source[0] if args.source else "goldman"
    adapter = get_adapter(src, cfg, session=None)

    print(f"文件: {path}  ({len(html):,} 字符)\n")
    for strategy in adapter.sc.get("parse_strategies", []):
        fn = getattr(adapter, f"_parse_{strategy}", None)
        if fn is None:
            print(f"{strategy:<16} 未实现")
            continue
        try:
            metas = fn(html)
        except Exception as e:  # noqa: BLE001
            print(f"{strategy:<16} 异常: {type(e).__name__}: {e}")
            continue
        print(f"{strategy:<16} 解析出 {len(metas)} 条")
        for m in metas[:5]:
            print(f"    {m.pub_date} | {m.title[:64]}")
            print(f"      {m.url}")
        if len(metas) > 5:
            print(f"    …还有 {len(metas) - 5} 条")
        print()
    return 0


# ----------------------------------------------------------------------
def _row_to_meta(row) -> ReportMeta:
    from datetime import date as _date
    pd = None
    if row["pub_date"]:
        try:
            pd = _date.fromisoformat(row["pub_date"])
        except ValueError:
            pd = None
    return ReportMeta(
        source=row["source"], uuid=row["uuid"] or "", title=row["title"] or "",
        url=row["url"], pdf_url=row["pdf_url"], pub_date=pd,
        summary=row["summary"], category=row["category"], authors=row["authors"],
        page_count=row["page_count"],
        restricted=bool(row["restricted"]) if "restricted" in row.keys() else False,
        parsed_by=row["parsed_by"] or "db",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gsr", description="头部投行研报抓取与中译工具")
    p.add_argument("--config", default=None, help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in [
        ("discover", cmd_discover, "只发现研报列表并入库，不下载"),
        ("fetch", cmd_fetch, "发现 + 下载正文与 PDF"),
        ("run", cmd_run, "fetch + translate 一条龙"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        _add_range_args(sp)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("translate", help="翻译已下载但未翻译的研报")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--provider", default=None,
                    help="临时指定 provider，覆盖配置"
                         "（claude/openai/deepseek/qwen/zhipu/modelscope）")
    sp.add_argument("--retry-failed", action="store_true",
                    help="忽略失败次数上限，强制重试失败过的条目")
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("list", help="查看库内研报")
    _add_range_args(sp)
    sp.add_argument("--status", default=None,
                    help="discovered / fetched / translated / failed")
    sp.add_argument("--keyword", default=None, help="标题或摘要关键词")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="显示失败的具体错误信息")
    sp.set_defaults(func=cmd_list, since="all")

    sp = sub.add_parser("retry", help="查看失败记录并重置失败计数，以便重跑")
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("status", help="统计信息")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("parse-test", help="用本地 HTML 文件测试解析器（不联网）")
    sp.add_argument("file")
    sp.add_argument("--source", action="append", default=None)
    sp.set_defaults(func=cmd_parse_test)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"配置加载失败: {e}")
        return 1
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        print("\n已中断。进度已存库，下次运行会续上。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
