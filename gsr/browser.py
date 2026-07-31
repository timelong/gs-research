"""浏览器层。

为什么必须用浏览器而不是 requests/httpx：
    gspublishing.com 全站挂了 Cloudflare managed challenge。纯 HTTP 请求
    （包括直接请求 .pdf）一律返回 403 "Just a moment..."。浏览器能访问是
    因为它执行了 JS 挑战并拿到了 cf_clearance cookie。

关键设计：persistent context
    用固定的 user_data_dir 启动，cf_clearance 会持久化在 profile 里，
    首次 headful 通过挑战之后可长期复用，不必每次都跑挑战。
    这个目录不要删，删了就得重新过挑战。

下载也必须走浏览器上下文
    用 page.request（继承页面的 cookie jar）而不是另起 httpx session，
    否则 cookie 不通、又是 403。
"""
from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

CF_CHALLENGE_MARKERS = (
    "Just a moment",
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "challenge-platform",
)


class RateLimiter:
    """朴素的间隔限速器。

    Cloudflare 对高频访问会从 managed challenge 升级到硬验证码，
    而验证码只能人工过。这里宁慢勿快。
    """

    def __init__(self, min_interval: float = 6.0, jitter: float = 3.0):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0

    def wait(self) -> None:
        target = self.min_interval + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < target:
            time.sleep(target - elapsed)
        self._last = time.monotonic()


class ChallengeBlocked(RuntimeError):
    """页面停在 Cloudflare 挑战上，没能拿到真实内容。"""


class BrowserSession:
    """封装 Playwright persistent context 的生命周期。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.profile_dir = cfg.browser_profile
        self.headless = bool(cfg.get("browser.headless", False))
        self.timeout_ms = int(cfg.get("browser.timeout_ms", 60000))
        # 必须是 domcontentloaded：站点有持续的埋点长轮询，networkidle 永不满足
        self.wait_until = cfg.get("browser.wait_until", "domcontentloaded")
        self.ready_timeout_ms = int(cfg.get("browser.ready_timeout_ms", 20000))
        self.settle_ms = int(cfg.get("browser.settle_ms", 2000))
        self.locale = cfg.get("browser.locale", "en-US")
        self.user_agent = cfg.get("browser.user_agent") or None
        # channel="chrome" 用系统已装的 Chrome，免去下载 Playwright 自带内核
        self.channel = cfg.get("browser.channel") or None
        self.executable_path = cfg.get("browser.executable_path") or None
        self.limiter = RateLimiter(
            float(cfg.get("fetch.min_interval_sec", 6.0)),
            float(cfg.get("fetch.jitter_sec", 3.0)),
        )
        self._pw = None
        self._ctx = None
        self._page = None

    # ------------------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()

        launch_kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            locale=self.locale,
            viewport={"width": 1440, "height": 900},
            args=[
                # 去掉最明显的自动化指纹
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        if self.user_agent:
            launch_kwargs["user_agent"] = self.user_agent
        # executable_path 优先于 channel
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
        elif self.channel:
            launch_kwargs["channel"] = self.channel

        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(self._launch_hint(e)) from e
        self._ctx.set_default_timeout(self.timeout_ms)

        # 抹掉 navigator.webdriver
        self._ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def _launch_hint(self, e: Exception) -> str:
        """浏览器启动失败时给出可操作的提示，而不是丢一句原始报错。"""
        msg = str(e)
        lines = [f"浏览器启动失败: {type(e).__name__}: {msg[:300]}", ""]

        if "executable doesn't exist" in msg.lower() or "not found" in msg.lower():
            if self.channel:
                lines += [
                    f"配置里 browser.channel = '{self.channel}'，"
                    f"但系统里找不到这个浏览器。",
                    "",
                    "可选做法：",
                    "  1) 装 Google Chrome，或把 channel 改成 'msedge' 用 Edge",
                    "  2) 在 browser.executable_path 里填浏览器的绝对路径，macOS 通常是：",
                    "     /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "  3) 把 channel 留空，改用 Playwright 自带内核：",
                    "     export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright",
                    "     playwright install chromium",
                ]
            else:
                lines += [
                    "还没安装 Playwright 的 Chromium 内核。两条路：",
                    "",
                    "  1) 用系统已装的 Chrome（推荐，免下载 ~150MB）：",
                    "     在 config.yaml 里设 browser.channel: \"chrome\"",
                    "",
                    "  2) 下载 Playwright 自带内核（国内建议配镜像）：",
                    "     export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright",
                    "     playwright install chromium",
                ]
        else:
            lines += [
                "如果是 profile 被占用，先关掉所有由本工具启动的浏览器窗口再重试。",
                f"profile 目录: {self.profile_dir}",
            ]
        return "\n".join(lines)

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("BrowserSession 未启动，请用 with 语句")
        return self._page

    # ------------------------------------------------------------------
    @staticmethod
    def _is_timeout(e: Exception) -> bool:
        # 不在模块顶层 import playwright（那样没装 playwright 就无法导入本模块，
        # 离线自检会跑不起来），所以按名字判断异常类型。
        return "timeout" in type(e).__name__.lower() \
            or "timeout" in str(e)[:200].lower()

    def _content_ready(self, ready_selector: Optional[str]) -> bool:
        """页面内容是否已经就绪（用于判断超时是否可以忽略）。"""
        try:
            if ready_selector:
                return self.page.query_selector(ready_selector) is not None
            return len(self.page.content()) > 5000
        except Exception:  # noqa: BLE001
            return False

    def goto(self, url: str, *, retries: Optional[int] = None,
             ready_selector: Optional[str] = None) -> str:
        """打开页面并返回渲染后的 HTML。

        ready_selector 是"内容已就绪"的判据。它有两个作用：
          1. DOM 就绪后继续等这个元素出现，确保拿到的是有内容的页面
          2. 万一等待条件超时，用它判断页面其实是否已经可用 ——
             这个站有持续的埋点请求，任何"等网络安静"的条件都可能超时，
             但文档本身早就到位了，此时重试纯属浪费（还多惹一次 Cloudflare）
        """
        retries = self.cfg.get("fetch.max_retries", 3) if retries is None else retries
        backoff = float(self.cfg.get("fetch.retry_backoff_sec", 15))
        last_err: Exception | None = None

        for attempt in range(1, int(retries) + 1):
            self.limiter.wait()
            try:
                try:
                    self.page.goto(url, wait_until=self.wait_until,
                                   timeout=self.timeout_ms)
                except Exception as nav_err:  # noqa: BLE001
                    if not self._is_timeout(nav_err):
                        raise
                    if not self._content_ready(ready_selector):
                        raise
                    print("  [!] 等待条件超时，但页面内容已就绪，继续处理"
                          "（该站有持续埋点请求，属正常现象）")

                # DOM 就绪后再确认关键元素出现
                if ready_selector:
                    try:
                        self.page.wait_for_selector(
                            ready_selector, timeout=self.ready_timeout_ms)
                    except Exception:  # noqa: BLE001
                        # 没等到不直接失败：可能是挑战页（下面会检测到），
                        # 也可能是页面结构变了（交给解析层降级处理）
                        print(f"  [!] 未等到关键元素 {ready_selector}，"
                              f"继续按现有内容处理")

                if self.settle_ms:
                    self.page.wait_for_timeout(self.settle_ms)
                html = self.page.content()

                if self._looks_like_challenge(html):
                    # headful 下人可以手动点掉验证码，多给点时间等它自己过
                    if not self.headless:
                        print(f"  [!] 检测到 Cloudflare 挑战，等待通过… "
                              f"(第 {attempt}/{retries} 次；如出现验证码请手动点击)")
                        try:
                            self.page.wait_for_function(
                                "() => !document.title.includes('Just a moment')",
                                timeout=90_000,
                            )
                            self.page.wait_for_timeout(self.settle_ms)
                            html = self.page.content()
                        except Exception:
                            pass
                    if self._looks_like_challenge(html):
                        raise ChallengeBlocked(f"仍被 Cloudflare 挑战拦住: {url}")

                return html

            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < int(retries):
                    sleep_for = backoff * attempt
                    print(f"  [!] 打开失败（{type(e).__name__}），"
                          f"{sleep_for:.0f}s 后重试: {e}")
                    time.sleep(sleep_for)

        raise RuntimeError(f"打开页面失败，已重试 {retries} 次: {url}") from last_err

    @staticmethod
    def _looks_like_challenge(html: str) -> bool:
        head = html[:6000]
        return any(m in head for m in CF_CHALLENGE_MARKERS)

    # ------------------------------------------------------------------
    def download(self, url: str, dest: Path, *,
                 retries: Optional[int] = None) -> Path:
        """用页面上下文下载二进制文件（PDF）。

        必须用 page.request —— 它复用页面的 cookie（含 cf_clearance）。
        另起独立 HTTP 客户端会因为缺 cookie 而 403。
        """
        retries = self.cfg.get("fetch.max_retries", 3) if retries is None else retries
        backoff = float(self.cfg.get("fetch.retry_backoff_sec", 15))
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None

        for attempt in range(1, int(retries) + 1):
            self.limiter.wait()
            try:
                resp = self.page.request.get(url, timeout=self.timeout_ms)
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                body = resp.body()
                # 内容嗅探：403 挑战页也会返回一堆 HTML，别当成 PDF 存下来
                if dest.suffix.lower() == ".pdf" and not body.startswith(b"%PDF"):
                    raise RuntimeError(
                        f"返回内容不是 PDF（可能是 Cloudflare 挑战页）: {url}"
                    )
                dest.write_bytes(body)
                return dest
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < int(retries):
                    time.sleep(backoff * attempt)

        raise RuntimeError(f"下载失败: {url}") from last_err

    # ------------------------------------------------------------------
    def scroll_to_load_all(self, *, max_scrolls: int = 25,
                           wait_ms: int = 1800,
                           item_selector: str | None = None,
                           stop_after_stale_rounds: int = 3) -> str:
        """反复滚到底触发前端加载更多，返回最终 HTML。

        不依赖任何未知的分页 API：只要页面是滚动加载，这个方法就有效。
        连续 stop_after_stale_rounds 轮条目数不增长即认为到底。
        """
        stale = 0
        prev_count = -1
        for i in range(max_scrolls):
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(wait_ms)

            if item_selector:
                count = len(self.page.query_selector_all(item_selector))
            else:
                count = int(self.page.evaluate("document.body.scrollHeight"))

            if count == prev_count:
                stale += 1
                if stale >= stop_after_stale_rounds:
                    break
            else:
                stale = 0
            prev_count = count

        return self.page.content()


@contextmanager
def open_session(cfg) -> Iterator[BrowserSession]:
    with BrowserSession(cfg) as s:
        yield s
