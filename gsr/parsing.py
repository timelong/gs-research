"""通用解析工具：内嵌 JSON 挖掘、日期解析、正文清洗。

内嵌 JSON 挖掘是这套东西里最关键的部分。gspublishing 的列表页把研报数据
以 JSON 形式内嵌在文档里（实测搜索命中形如 `distributionHeadline\\":\\"...`，
注意反斜杠 —— 说明它是被当作字符串二次转义嵌进外层 JSON 的）。

所以不能简单 json.loads 整个 script，需要：
  1. 准备多份候选文本：原始 HTML、各 <script> 内容、以及它们的反转义版本
  2. 在每份文本里定位关键字段名的出现位置
  3. 从该位置向外做花括号配对，切出一个平衡的 JSON 片段
  4. json.loads 验证，成功则递归收集所有"看起来像研报条目"的字典

这样无论它嵌几层、怎么转义，只要字段名还在就能挖出来。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional


# ----------------------------------------------------------------------
# 内嵌 JSON 挖掘
# ----------------------------------------------------------------------
def _unescape_variants(text: str) -> list[str]:
    """生成文本的反转义变体，应对多层嵌套转义。"""
    out = [text]
    cur = text
    for _ in range(2):  # 最多剥两层，够用了
        if '\\"' not in cur:
            break
        nxt = cur.replace('\\\\', '\\').replace('\\"', '"').replace('\\/', '/')
        if nxt == cur:
            break
        out.append(nxt)
        cur = nxt
    return out


def _balanced_json_at(text: str, pos: int,
                      max_span: int = 4_000_000) -> Optional[str]:
    """从 pos 出发，向左找最近的 '{'，再向右做花括号配对，返回平衡片段。

    会跳过字符串字面量里的花括号，避免被正文内容里的 { } 带偏。
    """
    start = text.rfind("{", max(0, pos - max_span), pos + 1)
    if start == -1:
        return None

    depth = 0
    in_str = False
    escaped = False
    i = start
    end_limit = min(len(text), start + max_span)

    while i < end_limit:
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


def _walk_dicts(obj: Any) -> Iterable[dict]:
    """深度遍历，产出所有字典节点。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _script_texts(html: str) -> list[str]:
    return re.findall(
        r"<script[^>]*>(.*?)</script>", html, flags=re.S | re.I
    )


def mine_embedded_json(
    html: str,
    probe_keys: Iterable[str],
) -> list[dict]:
    """在 HTML 里挖出所有包含 probe_keys 之一的 JSON 字典节点。

    返回去重后的字典列表（按 json 序列化结果去重）。
    """
    candidates: list[str] = []
    for base in [html, *_script_texts(html)]:
        candidates.extend(_unescape_variants(base))

    found: list[dict] = []
    seen: set[str] = set()

    for text in candidates:
        for key in probe_keys:
            for m in re.finditer(re.escape(f'"{key}"'), text):
                frag = _balanced_json_at(text, m.start())
                if not frag:
                    continue
                try:
                    parsed = json.loads(frag)
                except (json.JSONDecodeError, ValueError):
                    # 片段可能被截断或仍有转义残留，尝试向外再扩一层
                    wider = _balanced_json_at(text, max(0, m.start() - 1))
                    if not wider or wider == frag:
                        continue
                    try:
                        parsed = json.loads(wider)
                    except Exception:  # noqa: BLE001
                        continue

                for d in _walk_dicts(parsed):
                    if not any(k in d for k in probe_keys):
                        continue
                    sig = json.dumps(d, sort_keys=True, ensure_ascii=False)[:4000]
                    if sig in seen:
                        continue
                    seen.add(sig)
                    found.append(d)

    return found


def pick_field(d: dict, names: Iterable[str]) -> Any:
    """按候选字段名顺序取第一个非空值。字段命名会随站点改版变，所以都列上。"""
    for n in names:
        if n in d and d[n] not in (None, "", [], {}):
            return d[n]
    # 再试一次大小写不敏感匹配
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def stringify_authors(v: Any) -> Optional[str]:
    """作者字段可能是字符串、字符串列表或对象列表，统一成逗号分隔字符串。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                nm = pick_field(item, ["name", "fullName", "displayName"])
                if nm:
                    parts.append(str(nm).strip())
        return ", ".join(p for p in parts if p) or None
    return str(v)


# ----------------------------------------------------------------------
# 日期解析
# ----------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_loose(value: Any) -> Optional[date]:
    """尽最大努力把各种日期表示解析成 date。

    支持："10 Jul 2026 | 4:00pm"、ISO 8601、毫秒时间戳、"2026/07/10" 等。
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    # 时间戳（秒 / 毫秒）
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:      # 毫秒
            ts /= 1000.0
        try:
            return datetime.utcfromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # 纯数字字符串 -> 时间戳
    if re.fullmatch(r"\d{10,13}", s):
        return parse_date_loose(int(s))

    # ISO：2026-07-10 / 2026-07-10T16:00:00Z
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass

    # 2026/07/10
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass

    # "10 Jul 2026" / "10 July 2026"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b", s)
    if m:
        mon = _MONTHS.get(m[2][:4].lower().rstrip(".")) or _MONTHS.get(m[2][:3].lower())
        if mon:
            try:
                return date(int(m[3]), mon, int(m[1]))
            except ValueError:
                pass

    # "Jul 10, 2026"
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)
    if m:
        mon = _MONTHS.get(m[1][:4].lower().rstrip(".")) or _MONTHS.get(m[1][:3].lower())
        if mon:
            try:
                return date(int(m[3]), mon, int(m[2]))
            except ValueError:
                pass

    return None


# ----------------------------------------------------------------------
# 正文清洗
# ----------------------------------------------------------------------
def html_to_markdown(
    html: str,
    content_selectors: list[str],
    strip_selectors: list[str],
    *,
    keep_figure_placeholders: bool = True,
) -> str:
    """详情页 HTML -> 干净 Markdown。

    因为研报正文本身就是静态 HTML，这条路比解析 PDF 干净得多：
    段落、标题、列表、表格结构天然保留，不用跟分栏和图表排版较劲。
    """
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "lxml")

    for sel in strip_selectors:
        for node in soup.select(sel):
            node.decompose()

    # 图表转成占位标记，让译文里能看出"这里原本有张图"
    if keep_figure_placeholders:
        for img in soup.find_all("img"):
            alt = (img.get("alt") or "").strip()
            label = f"[图表：{alt}]" if alt else "[图表]"
            img.replace_with(soup.new_string(f"\n\n{label}\n\n"))

    container = None
    for sel in content_selectors:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            container = node
            break
    if container is None:
        container = soup.body or soup

    md = markdownify(str(container), heading_style="ATX", bullets="-")
    # 压缩多余空行
    md = re.sub(r"\n{4,}", "\n\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


_LINK_ONLY_RE = re.compile(r"^\s*[-*]?\s*\[([^\]]*)\]\(([^)]*)\)\s*$")


def strip_reader_chrome(md: str, cfg: dict) -> str:
    """清掉网页阅读器的界面文字残留。

    按节点剔除（strip_selectors）之后仍会有残留，因为有些控件文字
    并不在独立节点里。这一层按文本规则再清一遍。

    规则刻意保守 —— 只删明确是界面元素的行：整行等于已知标签、
    只含一个指向锚点/mailto/PDF 的链接、音频进度、报头元信息行。
    正文段落不可能长这样，所以不会误删内容。
    """
    if not cfg or not cfg.get("enabled", True):
        return md

    exact = {s.strip().lower() for s in cfg.get("drop_exact_lines", [])}
    lt = cfg.get("drop_link_only_targets", {}) or {}
    prefixes = tuple(lt.get("prefixes", []) or [])
    suffixes = tuple(lt.get("suffixes", []) or [])
    patterns = [re.compile(p) for p in cfg.get("drop_line_patterns", []) or []]
    drop_punct = bool(cfg.get("drop_punctuation_only", True))

    out: list[str] = []
    for raw in md.split("\n"):
        line = raw.strip()

        if not line:
            out.append("")
            continue

        # 1) 整行等于已知界面标签（去掉 markdown 的标题/列表符号后再比）
        bare = re.sub(r"^[#>\-*\s]+", "", line).strip()
        if bare.lower() in exact:
            continue

        # 2) 只含一个链接，且目标是锚点 / mailto / PDF —— 目录项或按钮
        m = _LINK_ONLY_RE.match(raw)
        if m:
            target = m.group(2).strip()
            if (prefixes and target.startswith(prefixes)) or \
               (suffixes and target.lower().endswith(suffixes)):
                continue
            # 链接文字本身就是界面标签时也删（如 [PDF](...)）
            if m.group(1).strip().lower() in exact:
                continue

        # 3) 正则命中的行（报头元信息、音频进度、mailto 长串）
        if any(p.search(line) for p in patterns):
            continue

        # 4) 只剩标点或符号的行（图标字形被剥掉文字后的残渣）
        if drop_punct and not re.search(r"[A-Za-z0-9一-鿿]", line):
            continue

        out.append(raw)

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def ensure_title_heading(md: str, title: str, *,
                         dedupe_window: int = 8) -> str:
    """确保正文以一个规范的 H1 标题开头，并去掉重复的报头残片。

    HTML 报头（系列名、标题、日期、作者栏）在提取后是散落的纯文本行，
    结构信息全丢了。与其保留那些残片，不如用元数据里的标题生成 H1，
    再把开头那几行已被标题涵盖的碎片删掉。

    例：元数据标题是
        "Commodity Views: From Energy to Metals: Why to Still Diversify…"
    而正文开头有两行散片
        "COMMODITY VIEWS"
        "From Energy to Metals: Why to Still Diversify Into Commodities"
    两者都是标题的子串，属于重复，删掉。

    只在开头 dedupe_window 行内、且要求是标题子串，所以不会误删正文。
    """
    if not title:
        return md

    ntitle = _norm(title)
    lines = md.split("\n")

    # 标题本身已作为某行出现 -> 就地提升为 H1，不另加
    for i, ln in enumerate(lines[:6]):
        if ntitle and ntitle == _norm(ln):
            lines[i] = f"# {ln.strip().lstrip('#').strip()}"
            return "\n".join(lines).strip()

    # 删掉开头几行里被标题涵盖的碎片
    kept: list[str] = []
    for i, ln in enumerate(lines):
        if i < dedupe_window:
            n = _norm(ln)
            if len(n) >= 8 and n in ntitle:
                continue
        kept.append(ln)

    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return f"# {title}\n\n{body}".strip()


def truncate_at_markers(text: str, markers: list[str],
                        min_fraction: float = 0.25) -> str:
    """在免责声明标记处截断。

    监管披露和免责声明往往占掉大半篇幅，翻译它们纯属浪费 token。

    取每个标记的**最后一次**出现（免责声明总在文末），并要求它位于文档
    min_fraction 之后 —— 这样既能命中真正的声明段，又不会因为正文里
    偶然提到"Disclosure Appendix"就把整篇砍掉。

    早先版本是"从文档中点往后 find 第一次出现"，标记刚好落在中点附近时
    会漏掉（find 的起点已经越过了它）。
    """
    if not markers or not text:
        return text

    threshold = len(text) * min_fraction
    cut = len(text)
    for mk in markers:
        idx = text.rfind(mk)
        if idx != -1 and idx >= threshold:
            cut = min(cut, idx)
    return text[:cut].rstrip()
