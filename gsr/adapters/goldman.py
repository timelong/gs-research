"""高盛研报 adapter。

三级降级解析（按顺序尝试，前一级拿到足够结果就停）：

  1. embedded_json —— 挖页面内嵌的 JSON（含 distributionHeadline 等字段）。
     字段最全（摘要、分类、作者、页数），但字段名会随改版变化。

  2. dom_testid —— 按 data-testid 定位 DOM。data-testid 是测试锚点，
     相对稳定；class 名（如 gs-uitk-c-hf7351）是编译产生的 hash，
     改版必失效，绝不依赖。

  3. href_regex —— 全文正则扒 /content/research/en/reports/YYYY/MM/DD/<uuid>.html。
     信息最少（只有链接和锚文本），但几乎不可能失效。作为最后防线。

日期一律取自 URL 路径，不用展示文本。原因（实测）：
    列表页显示的时间是按浏览器本地时区渲染的。同一篇研报
    publicationDateTime = 1783699236000 = 2026-07-10 16:00 UTC = 12:00 ET，
    在 UTC+8 下页面显示成 "11 Jul 2026 | 12:00am"，比实际发布日晚一天。
    而 URL 路径写的是 /2026/07/10/，与发布日一致。
    所以展示文本一律不采信，只作最后的兜底。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from ..models import FetchResult, ReportMeta
from ..parsing import (
    ensure_title_heading,
    html_to_markdown,
    mine_embedded_json,
    parse_date_loose,
    pick_field,
    stringify_authors,
    strip_reader_chrome,
    truncate_at_markers,
)
from .base import BaseAdapter, register


@register
class GoldmanAdapter(BaseAdapter):
    name = "goldman"

    # ------------------------------------------------------------------
    def __init__(self, cfg, source_cfg, session):
        super().__init__(cfg, source_cfg, session)
        self.base_url = self.sc.get("base_url", "https://www.gspublishing.com")
        self.url_re = re.compile(self.sc["report_url_pattern"])

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def list_reports(
        self,
        since: Optional[date] = None,
        until: Optional[date] = None,
        max_items: int = 100,
    ) -> list[ReportMeta]:
        pag = self.sc.get("pagination", {}) or {}
        mode = pag.get("mode", "page_param")

        if mode == "page_param":
            try:
                metas = self._list_via_page_param(pag, since, until, max_items)
            except Exception as e:  # noqa: BLE001
                fb = pag.get("fallback_mode")
                if not fb:
                    raise
                print(f"[goldman] page_param 翻页失败（{type(e).__name__}: {e}），"
                      f"回退到 {fb}")
                metas = self._list_via_scroll(pag)
        else:
            metas = self._list_via_scroll(pag)

        if not metas:
            raise RuntimeError(
                "解析无结果。可能是页面结构大改，或页面停在 Cloudflare 挑战上。"
                "建议把 browser.headless 设为 false 手动确认页面能正常打开，"
                "或用 `python -m gsr parse-test <存下来的.html>` 离线排查。"
            )

        # 去重（同一 uuid 可能跨页或跨策略重复出现）
        uniq: dict[str, ReportMeta] = {}
        for m in metas:
            uniq.setdefault(m.uuid or m.url, m)
        metas = list(uniq.values())

        filtered = [m for m in metas if self._in_range(m.pub_date, since, until)]
        filtered.sort(key=lambda m: m.pub_date or date.min, reverse=True)

        skipped = len(metas) - len(filtered)
        if skipped:
            print(f"[goldman] 日期区间外跳过 {skipped} 条")

        n_restricted = sum(1 for m in filtered if m.restricted)
        if n_restricted:
            print(f"[goldman] 其中 {n_restricted} 条标记为有访问限制，"
                  f"正文可能抓不到")

        return filtered[:max_items]

    # ------------------------------------------------------------------
    def _list_via_page_param(self, pag: dict,
                             since: Optional[date],
                             until: Optional[date],
                             max_items: int) -> list[ReportMeta]:
        """走 search.html?page=N 翻页。

        sort=time 是时间降序，所以可以做日期早停：一旦某页里最旧的条目
        已经早于 since，后面的页只会更旧，直接停。抓"今年以来"时
        这一条能省掉大量无用请求（也就是少惹 Cloudflare）。
        """
        base = pag["search_url"]
        tpl = pag["query_template"]
        start = int(pag.get("start_page", 1))
        max_pages = int(pag.get("max_pages", 40))
        early_stop = bool(pag.get("early_stop_on_date", True))
        stop_empty = bool(pag.get("stop_on_empty_page", True))

        all_metas: list[ReportMeta] = []
        seen: set[str] = set()

        sel = self.sc.get("search_dom_selectors", {})
        total_items = total_pages = None

        for page in range(start, start + max_pages):
            url = f"{base}?{tpl.format(page=page)}"
            suffix = f"/{total_pages}" if total_pages else ""
            print(f"[goldman] 第 {page}{suffix} 页…")
            html = self.session.goto(
                url, ready_selector=self.sc.get("ready_selector"))

            # 首页顺便读一下结果总数和总页数：用于日志，也用于收紧翻页上限
            if total_items is None:
                total_items, total_pages = self._parse_result_total(
                    html, sel.get("result_summary", ""), sel.get("pager", ""))
                if total_items or total_pages:
                    print(f"[goldman] 该检索条件共 {total_items or '?'} 条 / "
                          f"{total_pages or '?'} 页")

            metas = self._parse_with_strategies(html, quiet=(page > start))

            if not metas:
                if stop_empty:
                    print(f"[goldman] 第 {page} 页无条目，翻页结束")
                break

            fresh = [m for m in metas if (m.uuid or m.url) not in seen]
            if not fresh:
                # 翻页参数失效时页面会一直返回同一批内容，避免空转
                print(f"[goldman] 第 {page} 页内容与前页重复，翻页结束")
                break
            for m in fresh:
                seen.add(m.uuid or m.url)
            all_metas.extend(fresh)

            dates = [m.pub_date for m in metas if m.pub_date]
            oldest = min(dates) if dates else None
            print(f"    +{len(fresh)} 条（累计 {len(all_metas)}）"
                  + (f"，本页最旧 {oldest}" if oldest else ""))

            if early_stop and since and oldest and oldest < since:
                print(f"[goldman] 本页最旧条目 {oldest} 已早于 {since}，停止翻页")
                break

            in_range_count = sum(
                1 for m in all_metas if self._in_range(m.pub_date, since, until)
            )
            if in_range_count >= max_items:
                print(f"[goldman] 已达上限 {max_items} 条，停止翻页")
                break

            if total_pages and page >= total_pages:
                print(f"[goldman] 已到最后一页（共 {total_pages} 页）")
                break

        return all_metas

    def _list_via_scroll(self, pag: dict) -> list[ReportMeta]:
        """兜底：在 entry_url 上滚动加载。"""
        entry = self.sc["entry_url"]
        print(f"[goldman] 打开列表页: {entry}")
        self.session.goto(entry, ready_selector=self.sc.get("ready_selector"))
        print("[goldman] 滚动加载更多条目…")
        html = self.session.scroll_to_load_all(
            max_scrolls=int(pag.get("max_scrolls", 25)),
            wait_ms=int(pag.get("scroll_wait_ms", 1800)),
            item_selector=self.sc.get("dom_selectors", {}).get("item_container"),
            stop_after_stale_rounds=int(pag.get("stop_after_stale_rounds", 3)),
        )
        return self._parse_with_strategies(html)

    def _parse_with_strategies(self, html: str,
                               quiet: bool = False) -> list[ReportMeta]:
        """按配置顺序跑三级解析，第一级有结果就停。"""
        for strategy in self.sc.get("parse_strategies", ["href_regex"]):
            fn = getattr(self, f"_parse_{strategy}", None)
            if fn is None:
                continue
            try:
                metas = fn(html)
            except Exception as e:  # noqa: BLE001
                print(f"[goldman] 策略 {strategy} 异常"
                      f"（{type(e).__name__}: {e}），降级")
                continue
            if metas:
                if not quiet:
                    print(f"[goldman] 解析策略: {strategy}（{len(metas)} 条）")
                    if strategy.startswith("href_regex"):
                        print("[goldman] 注意：这是最末级兜底策略，"
                              "只能拿到标题/链接/日期，"
                              "页数、分类、作者、受限标记均缺失。"
                              "若长期落到这一级，说明页面结构已变，"
                              "建议存下页面用 parse-test 排查。")
                return metas
            if not quiet:
                print(f"[goldman] 策略 {strategy} 无结果，降级")
        return []

    @staticmethod
    def _in_range(d: Optional[date],
                  since: Optional[date],
                  until: Optional[date]) -> bool:
        if d is None:
            # 没日期的条目保留，交给后续人工判断，不静默丢弃
            return True
        if since and d < since:
            return False
        if until and d > until:
            return False
        return True

    # ------------------------------------------------------------------
    # 策略 1：内嵌 JSON
    # ------------------------------------------------------------------
    def _parse_embedded_json(self, html: str) -> list[ReportMeta]:
        fmap = self.sc.get("json_field_map", {})
        probe_keys = list(fmap.get("title", ["distributionHeadline"]))
        blobs = mine_embedded_json(html, probe_keys)
        if not blobs:
            return []

        out: list[ReportMeta] = []
        for d in blobs:
            title = pick_field(d, fmap.get("title", []))
            if not title or not isinstance(title, str):
                continue

            raw_url = pick_field(d, fmap.get("url", []))
            url = self._normalize_url(raw_url) if raw_url else None

            # JSON 里没有可用链接时，尝试用 id 字段拼；仍失败则丢弃这条
            if not url:
                continue

            info = self._parse_report_url(url)
            if not info:
                continue

            # 日期：URL 路径优先，JSON 时间戳（按 UTC）作次选。
            # 二者实测一致；展示文本受时区影响，不参与。
            ts_date = parse_date_loose(pick_field(d, fmap.get("date", [])))
            out.append(ReportMeta(
                source=self.name,
                uuid=info["uuid"],
                title=title.strip(),
                url=self._as_html_url(url, info),
                pdf_url=self._as_pdf_url(url, info),
                pub_date=info["date"] or ts_date,
                summary=(pick_field(d, fmap.get("summary", [])) or None),
                category=self._clean_category(
                    pick_field(d, fmap.get("category", []))),
                authors=self._authors_from_json(d, fmap),
                page_count=self._as_int(pick_field(d, fmap.get("page_count", []))),
                restricted=self._is_restricted(d, fmap),
                parsed_by="embedded_json",
            ))
        return out

    # ------------------------------------------------------------------
    def _authors_from_json(self, d: dict, fmap: dict) -> Optional[str]:
        """leadAuthor 只给主笔一人，hasMultipleAuthors 为真时补"等"。"""
        raw = pick_field(d, fmap.get("authors", []))
        s = stringify_authors(raw)
        if not s:
            return None
        flag = self.sc.get("multi_author_flag", "hasMultipleAuthors")
        if d.get(flag):
            s += self.sc.get("multi_author_suffix", " 等")
        return s

    def _clean_category(self, raw) -> Optional[str]:
        """sourceDisplayName 形如 "Research | Portfolio Strategy"，取后半段。"""
        if not raw or not isinstance(raw, str):
            return raw or None
        sep = self.sc.get("category_split")
        if sep and sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            if parts:
                return parts[-1] if self.sc.get("category_take_last", True) \
                    else parts[0]
        return raw.strip() or None

    @staticmethod
    def _is_restricted(d: dict, fmap: dict) -> bool:
        """restrictionDetails / securityRestrictionMap 非 null 即有访问限制。"""
        for k in fmap.get("restriction", []):
            if d.get(k) not in (None, "", [], {}):
                return True
        return False

    # ------------------------------------------------------------------
    # 策略 2：search.html 表格
    # ------------------------------------------------------------------
    def _parse_search_table(self, html: str) -> list[ReportMeta]:
        """解析 search.html 的表格布局。

        这一页的元数据最全 —— 除了 public.html 有的分类/作者/页数，
        还多出一段摘要摘录（SearchResults__colExtract），
        以及一个原始毫秒时间戳（SearchResults__hiddenEl）。

        class 名在这一页是语义化的 SearchResults__xxx，不是编译 hash，
        所以按 class 定位是安全的。
        """
        from bs4 import BeautifulSoup

        sel = self.sc.get("search_dom_selectors", {})
        soup = BeautifulSoup(html, "lxml")

        anchors = soup.select(sel.get("headline", "a.SearchResults__headline"))
        if not anchors:
            return []

        out: list[ReportMeta] = []
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            info = self._parse_report_url(href)
            if not info:
                continue
            url = self._normalize_url(href)

            row = a.find_parent("tr") or a.parent

            title = a.get_text(" ", strip=True)
            if not title:
                continue

            # 原始时间戳（UTC 毫秒）。仍以 URL 路径日期为准，二者实测一致。
            ts_node = row.select_one(sel.get("timestamp", "")) if row else None
            ts_date = None
            if ts_node:
                ts_date = parse_date_loose(ts_node.get_text(strip=True))

            meta_node = row.select_one(sel.get("meta_text", "")) if row else None
            meta_text = meta_node.get_text(" ", strip=True) if meta_node else ""
            category, authors = self._split_meta_text(meta_text)

            pages_node = row.select_one(sel.get("pages_cell", "")) if row else None
            page_count = self._as_int(
                (pages_node.get_text(strip=True) if pages_node else "") or None)

            ex_node = row.select_one(sel.get("extract", "")) if row else None
            summary = ex_node.get_text(" ", strip=True) if ex_node else None

            out.append(ReportMeta(
                source=self.name,
                uuid=info["uuid"],
                title=title,
                url=self._as_html_url(url, info),
                pdf_url=self._as_pdf_url(url, info),
                pub_date=info["date"] or ts_date,
                summary=summary or None,
                category=category,
                authors=authors,
                page_count=page_count,
                parsed_by="search_table",
            ))
        return out

    def _split_meta_text(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """拆 "Research | Portfolio Strategy -  作者A,  作者B, and others…"

        返回 (分类, 作者)。任一段缺失就返回 None，不硬凑。
        """
        if not text:
            return None, None
        sep = self.sc.get("meta_text_split", " - ")
        left, right = (text.split(sep, 1) + [""])[:2] if sep in text else (text, "")

        category = self._clean_category(left.strip()) if left.strip() else None

        authors = right.strip()
        if not authors:
            return category, None

        multi = False
        for mk in self.sc.get("authors_tail_markers", []):
            if authors.endswith(mk):
                authors = authors[: -len(mk)].rstrip().rstrip(",").rstrip()
                multi = True
                break
        # 作者之间有多余空格，压一下
        authors = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", authors)).strip(", ")
        if not authors:
            return category, None
        if multi:
            authors += self.sc.get("multi_author_suffix", " 等")
        return category, authors

    @staticmethod
    def _parse_result_total(html: str, summary_sel: str,
                            pager_sel: str) -> tuple[Optional[int], Optional[int]]:
        """从 "0 - 25 of 654" 和 "1 of 27" 里读出总条数与总页数。

        只用于日志显示和收紧翻页上限，解析失败不影响主流程。
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            total = pages = None
            if summary_sel:
                node = soup.select_one(summary_sel)
                if node:
                    m = re.search(r"of\s+([\d,]+)", node.get_text(" ", strip=True))
                    if m:
                        total = int(m[1].replace(",", ""))
            if pager_sel:
                node = soup.select_one(pager_sel)
                if node:
                    m = re.search(r"\b\d+\s+of\s+(\d+)\b",
                                  node.get_text(" ", strip=True))
                    if m:
                        pages = int(m[1])
            return total, pages
        except Exception:  # noqa: BLE001
            return None, None

    # ------------------------------------------------------------------
    # 策略 3：data-testid DOM
    # ------------------------------------------------------------------
    def _parse_dom_testid(self, html: str) -> list[ReportMeta]:
        from bs4 import BeautifulSoup

        sel = self.sc.get("dom_selectors", {})
        soup = BeautifulSoup(html, "lxml")

        items = soup.select(sel.get("item_container", ""))
        if not items:
            return []

        out: list[ReportMeta] = []
        for it in items:
            a = it.select_one(sel.get("title_anchor", "a")) or it.find("a", href=True)
            if not a or not a.get("href"):
                continue
            url = self._normalize_url(a["href"])
            info = self._parse_report_url(url)
            if not info:
                continue

            title = a.get_text(" ", strip=True)
            if not title:
                title = it.get_text(" ", strip=True)[:200]

            meta_node = it.select_one(sel.get("metadata", ""))
            meta_text = meta_node.get_text(" | ", strip=True) if meta_node else ""

            # 日期只用 URL 路径。展示文本按浏览器时区渲染，会差一天。
            # 仅当 URL 里解不出日期时才退而用展示文本。
            pub = info["date"] or parse_date_loose(meta_text)

            out.append(ReportMeta(
                source=self.name,
                uuid=info["uuid"],
                title=title,
                url=self._as_html_url(url, info),
                pdf_url=self._as_pdf_url(url, info),
                pub_date=pub,
                page_count=self._page_count_from_text(meta_text),
                category=self._category_from_text(meta_text),
                authors=self._authors_from_text(meta_text),
                parsed_by="dom_testid",
            ))
        return out

    # ------------------------------------------------------------------
    # 展示文本形如：
    #   "11 Jul 2026 | 12:00am | 35pg | Research | Portfolio Strategy
    #    - Christian Mueller-Glissmann, CFA and others"
    @staticmethod
    def _category_from_text(text: str) -> Optional[str]:
        m = re.search(r"\|\s*Research\s*\|\s*([^|\-]+?)\s*(?:-|\||$)", text)
        return m[1].strip() if m else None

    @staticmethod
    def _authors_from_text(text: str) -> Optional[str]:
        m = re.search(r"-\s*([^|]+?)(?:\s+and\s+others)?\s*$", text)
        if not m:
            return None
        s = m[1].strip()
        if "and others" in text[m.start():]:
            s += " 等"
        return s or None

    # ------------------------------------------------------------------
    # 策略 3：href 正则兜底
    # ------------------------------------------------------------------
    def _parse_href_regex(self, html: str) -> list[ReportMeta]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        out: dict[str, ReportMeta] = {}

        for a in soup.find_all("a", href=True):
            info = self._parse_report_url(a["href"])
            if not info:
                continue
            url = self._normalize_url(a["href"])
            title = a.get_text(" ", strip=True)
            if not title:
                # 锚文本为空时，往上找一层容器取文本
                parent = a.find_parent()
                title = parent.get_text(" ", strip=True)[:200] if parent else ""
            uid = info["uuid"]
            if uid in out and len(out[uid].title) >= len(title):
                continue
            out[uid] = ReportMeta(
                source=self.name,
                uuid=uid,
                title=title or f"untitled-{uid[:8]}",
                url=self._as_html_url(url, info),
                pdf_url=self._as_pdf_url(url, info),
                pub_date=info["date"],
                parsed_by="href_regex",
            )

        # 连 <a> 都没有时，直接在原始文本里扒
        if not out:
            for m in self.url_re.finditer(html):
                info = self._info_from_match(m)
                if not info:
                    continue
                url = self._normalize_url(m.group(0))
                out.setdefault(info["uuid"], ReportMeta(
                    source=self.name,
                    uuid=info["uuid"],
                    title=f"untitled-{info['uuid'][:8]}",
                    url=self._as_html_url(url, info),
                    pdf_url=self._as_pdf_url(url, info),
                    pub_date=info["date"],
                    parsed_by="href_regex_raw",
                ))

        return list(out.values())

    # ------------------------------------------------------------------
    # 抓取单篇
    # ------------------------------------------------------------------
    def fetch_report(self, meta: ReportMeta, out_dir: Path) -> FetchResult:
        result = FetchResult(meta=meta)
        subdir = out_dir / meta.archive_subdir()
        subdir.mkdir(parents=True, exist_ok=True)
        stem = f"{meta.pub_date or 'nodate'}_{meta.safe_title}_{meta.uuid[:8]}"

        # --- 正文 HTML ---
        try:
            html = self.session.goto(
                meta.url,
                ready_selector=self.sc.get("detail_ready_selector"),
            )
            if self.cfg.get("fetch.save_raw_html", True):
                p = subdir / f"{stem}.raw.html"
                p.write_text(html, encoding="utf-8")
                result.html_path = str(p)

            body_md = self.extract_body(html, title=meta.title)
            if body_md:
                p = subdir / f"{stem}.original.md"
                p.write_text(
                    self._front_matter(meta) + body_md, encoding="utf-8"
                )
                # 翻译层读的是这个文件
                result.html_path = str(p)
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"正文抓取失败: {e}")

        # --- PDF 归档 ---
        if self.cfg.get("fetch.download_pdf", True) and meta.pdf_url:
            try:
                p = subdir / f"{stem}.pdf"
                self.session.download(meta.pdf_url, p)
                result.pdf_path = str(p)
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"PDF 下载失败: {e}")

        return result

    # ------------------------------------------------------------------
    def extract_body(self, html: str, title: str = "") -> str:
        """详情页 HTML -> 干净的 Markdown 正文。

        四步：节点剔除 -> 转 Markdown -> 文本层清界面残留 -> 截断免责声明。
        """
        md = html_to_markdown(
            html,
            content_selectors=self.sc.get("content_selectors", ["article", "main"]),
            strip_selectors=self.sc.get("strip_selectors", ["script", "style"]),
            keep_figure_placeholders=bool(
                self.cfg.get("translate.keep_figure_placeholders", True)
            ),
        )
        md = strip_reader_chrome(md, self.sc.get("reader_chrome", {}))
        if self.sc.get("truncate_at_disclaimer", True):
            md = truncate_at_markers(md, self.sc.get("disclaimer_markers", []))
        if title:
            md = ensure_title_heading(md, title)
        return md

    @staticmethod
    def _front_matter(meta: ReportMeta) -> str:
        lines = [
            "---",
            f"source: {meta.source}",
            f"uuid: {meta.uuid}",
            f"title: {meta.title!r}",
            f"pub_date: {meta.pub_date or ''}",
            f"url: {meta.url}",
            f"pdf_url: {meta.pdf_url or ''}",
            f"category: {meta.category or ''}",
            f"authors: {meta.authors or ''}",
            f"parsed_by: {meta.parsed_by}",
            "---",
            "",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # URL 工具
    # ------------------------------------------------------------------
    def _normalize_url(self, raw: str) -> str:
        raw = str(raw).strip().replace("\\/", "/")
        if raw.startswith("//"):
            return "https:" + raw
        if raw.startswith("http"):
            return raw
        return urljoin(self.base_url + "/", raw.lstrip("/"))

    def _parse_report_url(self, url: str) -> Optional[dict]:
        m = self.url_re.search(str(url).replace("\\/", "/"))
        return self._info_from_match(m) if m else None

    @staticmethod
    def _info_from_match(m) -> Optional[dict]:
        try:
            d = date(int(m.group("year")), int(m.group("month")),
                     int(m.group("day")))
        except (ValueError, IndexError):
            d = None
        return {
            "uuid": m.group("uuid").lower(),
            "date": d,
            "lang": m.group("lang"),
            "ext": m.group("ext"),
            "path": m.group(0),
        }

    def _as_html_url(self, url: str, info: dict) -> str:
        path = info["path"]
        return self._normalize_url(
            url.replace(path, re.sub(r"\.(pdf|html)$", ".html", path))
        )

    def _as_pdf_url(self, url: str, info: dict) -> str:
        path = info["path"]
        return self._normalize_url(
            url.replace(path, re.sub(r"\.(pdf|html)$", ".pdf", path))
        )

    @staticmethod
    def _as_int(v) -> Optional[int]:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _page_count_from_text(text: str) -> Optional[int]:
        # 实测展示文本用的是 "35pg" 这种缩写，不是 "35 pages"
        m = re.search(r"(\d+)\s*(?:pages|page|pgs|pg|pp)\b", text, re.I)
        return int(m[1]) if m else None
