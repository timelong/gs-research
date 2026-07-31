"""浏览器层逻辑自检（不启真实浏览器，用假 page 对象）。

    python -m tests.test_browser_logic

重点覆盖那个把首次运行卡死的坑：
    这个站挂了 Datadog RUM 和 LaunchDarkly，会持续发埋点请求，
    networkidle 永远不满足 -> goto 必然超时 -> 三次重试全废。
    但文档其实早就加载好了。所以超时后必须先检查内容是否已就绪，
    就绪就继续，别重试。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsr.browser import BrowserSession, ChallengeBlocked  # noqa: E402
from gsr.config import load_config                        # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'  ok  ' if cond else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not cond else ""))


class FakeTimeout(Exception):
    """模拟 playwright 的 TimeoutError（按类名识别，不依赖真实库）。"""
    def __init__(self, msg="Page.goto: Timeout 45000ms exceeded."):
        super().__init__(msg)


FakeTimeout.__name__ = "TimeoutError"


CONTENT_OK = "<html><body>" + ("<div>real content</div>" * 500) + "</body></html>"
CONTENT_CHALLENGE = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><script>window._cf_chl_opt={};</script></body></html>"
)


class FakePage:
    """只实现 goto / content / query_selector / wait_for_* 这几个用到的方法。"""

    def __init__(self, *, raise_on_goto=None, html=CONTENT_OK,
                 has_selector=True):
        self.raise_on_goto = raise_on_goto
        self.html = html
        self.has_selector = has_selector
        self.goto_calls = 0
        self.waited_selectors: list[str] = []

    def goto(self, url, **kw):
        self.goto_calls += 1
        if self.raise_on_goto:
            raise self.raise_on_goto

    def content(self):
        return self.html

    def query_selector(self, sel):
        return object() if self.has_selector else None

    def wait_for_selector(self, sel, timeout=None):
        self.waited_selectors.append(sel)
        if not self.has_selector:
            raise FakeTimeout(f"selector {sel} not found")
        return object()

    def wait_for_timeout(self, ms):
        return None

    def wait_for_function(self, fn, timeout=None):
        return None

    def evaluate(self, script):
        return 1000

    def query_selector_all(self, sel):
        return [object()] * 10


def make_session(cfg, page) -> BrowserSession:
    s = BrowserSession(cfg)
    s._page = page
    # 关掉限速，测试不需要真等 6 秒
    s.limiter.min_interval = 0
    s.limiter.jitter = 0
    return s


def main() -> int:
    cfg = load_config()

    print("=== 1. 配置默认值（防回退到 networkidle）===")
    s = BrowserSession(cfg)
    check("wait_until 不是 networkidle", s.wait_until != "networkidle",
          f"实际 {s.wait_until!r}")
    check("wait_until 为 domcontentloaded",
          s.wait_until == "domcontentloaded", f"实际 {s.wait_until!r}")
    check("ready_timeout_ms 已配置", s.ready_timeout_ms > 0,
          str(s.ready_timeout_ms))

    print("\n=== 2. 超时但内容已就绪 -> 继续，不重试（核心回归点）===")
    p = FakePage(raise_on_goto=FakeTimeout(), html=CONTENT_OK,
                 has_selector=True)
    sess = make_session(cfg, p)
    try:
        html = sess.goto("https://x/", ready_selector="#list")
        check("返回了页面内容", "real content" in html)
        check("只调用 goto 一次（没有无谓重试）", p.goto_calls == 1,
              f"实际 {p.goto_calls} 次")
    except Exception as e:  # noqa: BLE001
        check("超时但内容就绪时应继续", False, f"{type(e).__name__}: {e}")

    print("\n=== 3. 超时且内容确实没就绪 -> 正常重试后失败 ===")
    p = FakePage(raise_on_goto=FakeTimeout(), html="<html></html>",
                 has_selector=False)
    sess = make_session(cfg, p)
    sess.cfg.raw.setdefault("fetch", {})["retry_backoff_sec"] = 0
    try:
        sess.goto("https://x/", retries=2, ready_selector="#list")
        check("内容未就绪时应抛错", False, "没抛错")
    except RuntimeError:
        check("内容未就绪时抛 RuntimeError", True)
        check("按 retries 次数重试", p.goto_calls == 2, f"实际 {p.goto_calls} 次")
    except Exception as e:  # noqa: BLE001
        check("内容未就绪时抛 RuntimeError", False, type(e).__name__)

    print("\n=== 4. 非超时异常不走「内容已就绪」的宽容路径 ===")
    p = FakePage(raise_on_goto=ValueError("net::ERR_CONNECTION_REFUSED"),
                 html=CONTENT_OK, has_selector=True)
    sess = make_session(cfg, p)
    sess.cfg.raw.setdefault("fetch", {})["retry_backoff_sec"] = 0
    try:
        sess.goto("https://x/", retries=1, ready_selector="#list")
        check("连接类错误应抛出", False, "被错误地放过了")
    except RuntimeError:
        check("连接类错误应抛出", True)

    print("\n=== 5. ready_selector 会被真正等待 ===")
    p = FakePage(html=CONTENT_OK, has_selector=True)
    sess = make_session(cfg, p)
    sess.goto("https://x/", ready_selector="#list-container")
    check("wait_for_selector 被调用", p.waited_selectors == ["#list-container"],
          str(p.waited_selectors))

    print("\n=== 6. 等不到 ready_selector 不直接失败（交给解析层降级）===")
    p = FakePage(html=CONTENT_OK, has_selector=False)
    sess = make_session(cfg, p)
    try:
        html = sess.goto("https://x/", ready_selector="#nope")
        check("仍返回内容供解析层降级处理", "real content" in html)
    except Exception as e:  # noqa: BLE001
        check("等不到元素不应直接失败", False, f"{type(e).__name__}: {e}")

    print("\n=== 7. Cloudflare 挑战页仍能被识别 ===")
    check("挑战页特征命中",
          BrowserSession._looks_like_challenge(CONTENT_CHALLENGE))
    check("正常页面不误判",
          not BrowserSession._looks_like_challenge(CONTENT_OK))
    p = FakePage(html=CONTENT_CHALLENGE, has_selector=True)
    sess = make_session(cfg, p)
    sess.headless = True   # headless 下不等人工点验证码，直接判失败
    sess.cfg.raw.setdefault("fetch", {})["retry_backoff_sec"] = 0
    try:
        sess.goto("https://x/", retries=1)
        check("挑战页应报错", False, "被当成正常页面了")
    except RuntimeError as e:
        check("挑战页应报错", True)
        check("错误信息提到 Cloudflare",
              "Cloudflare" in str(e) or "挑战" in str(e)
              or isinstance(e.__cause__, ChallengeBlocked), str(e)[:100])

    print("\n=== 8. 超时判定 ===")
    check("识别 playwright TimeoutError", BrowserSession._is_timeout(FakeTimeout()))
    check("识别消息里含 Timeout 的异常",
          BrowserSession._is_timeout(Exception("Timeout 45000ms exceeded")))
    check("不误判普通异常",
          not BrowserSession._is_timeout(ValueError("bad selector")))

    print("\n=== 9. 站点配置里的就绪判据 ===")
    sc = cfg.source_config("goldman")
    check("列表页 ready_selector 已配置", bool(sc.get("ready_selector")),
          str(sc.get("ready_selector")))
    check("详情页 detail_ready_selector 已配置",
          bool(sc.get("detail_ready_selector")))
    check("ready_selector 指向 data-testid 而非 hash class",
          "data-testid" in sc.get("ready_selector", ""),
          str(sc.get("ready_selector")))

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
