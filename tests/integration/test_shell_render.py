"""Integration tests for the iframe shell (Spec A), driven on real chromium.

These assert the structural guarantees that unit tests with fakes cannot: the
site sits strictly below the bar, no second bar/cursor leaks into the framed
site, and header stripping + a redirect chain leave the pill at the final URL.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import Frame, Page, async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.models.config import ChromeConfig, CursorConfig
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.slide import SlideOverlay

pytestmark = pytest.mark.integration

HEIGHT = 56
VIEWPORT = {"width": 400, "height": 400}


async def _setup_shell(browser, chrome_config: ChromeConfig) -> tuple[Page, Frame, Chrome]:
    """Reproduce ``render/_run.py``'s main-window setup; return (shell page, site frame)."""

    context = await browser.new_context(
        viewport=VIEWPORT,
        bypass_csp=True,
        service_workers="block",
    )
    overlay = Overlay(CursorConfig())
    slide = SlideOverlay()
    chrome = Chrome(chrome_config)
    # Mirror render/_run.py's init-script order: cursor (overlay) -> slide -> chrome,
    # so slide.js's isTop guard reads the real window.top before chrome.js
    # shadows it (see render/_run.py's cursor/slide-before-chrome contract comment).
    await overlay.install_context(context)
    await slide.install_context(context)
    await chrome.install_context(context)
    await install_framing(context, shell_origin="https://guidebot.shell/")
    page = await context.new_page()
    site_frame = await chrome.install_shell(page)
    return page, site_frame, chrome


@pytest.fixture
async def browser() -> AsyncIterator:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


ADVERSARIAL_SITE = (
    "data:text/html,"
    "<style>html,body{margin:0}"
    "header{position:fixed;top:0;left:0;right:0;height:80px;"
    "background:rgb(255,0,0);z-index:99999}"
    ".hero{height:100vh;background:rgb(0,0,255)}</style>"
    "<header>H</header><div class='hero'></div>"
)


async def test_site_sits_strictly_below_the_bar_under_adversarial_css(browser) -> None:
    page, site_frame, _ = await _setup_shell(browser, ChromeConfig(enabled=True, height=HEIGHT))
    await site_frame.goto(ADVERSARIAL_SITE)

    # The iframe — which clips ALL site pixels — begins exactly at the bar height,
    # so a fixed;top:0 header and a 100vh hero cannot paint into the bar strip.
    box = await page.locator("iframe#guidebot-site").bounding_box()
    assert box == {"x": 0.0, "y": float(HEIGHT), "width": 400.0, "height": 400.0 - HEIGHT}

    # Above the bar height the topmost element is the shell bar, not the iframe.
    hit = await page.evaluate(
        """h => {
            const top = document.elementFromPoint(200, 10);
            const below = document.elementFromPoint(200, h + 40);
            return {
                topIsIframe: top?.id === "guidebot-site",
                belowIsIframe: below?.id === "guidebot-site",
            };
        }""",
        HEIGHT,
    )
    assert hit == {"topIsIframe": False, "belowIsIframe": True}

    # Inside the frame the adversarial header is pinned to frame-top 0 — i.e. it
    # would render at shell y == HEIGHT, below the bar, never above it.
    header_top = await site_frame.evaluate(
        "() => document.querySelector('header').getBoundingClientRect().top"
    )
    assert header_top == 0


async def test_no_second_bar_or_cursor_inside_the_framed_site(browser) -> None:
    page, site_frame, _ = await _setup_shell(browser, ChromeConfig(enabled=True, height=HEIGHT))
    await site_frame.goto("data:text/html,<main>framed</main>")

    # The shell owns exactly one bar and one cursor.
    assert await page.locator("[data-guidebot-shell-bar]").count() == 1
    assert await page.locator("[data-guidebot-cursor]").count() == 1

    # None of the overlays leak into the framed site, and no padding-top is
    # injected on its <html> (the legacy padding heuristic must not run here).
    # __guidebot_slide is checked here alongside chromeApi/cursorApi: slide.js's
    # isTop guard (like cursor.js's) must bail inside the framed site and never
    # install its API there. Note this locks in the outcome, not the ordering
    # itself — real Chromium already makes chrome.js's window.top shadowing a
    # no-op for cross-origin frames, so this passes regardless of init-script
    # order; the order contract (cursor/overlay -> slide -> chrome, see
    # render/_run.py) is separately covered by a registration-order spy test in
    # tests/unit/recorder/test_render.py.
    leaked = await site_frame.evaluate(
        """() => ({
            legacyBar: document.querySelectorAll("[data-guidebot-chrome]").length,
            shellBar: document.querySelectorAll("[data-guidebot-shell-bar]").length,
            cursor: document.querySelectorAll("[data-guidebot-cursor]").length,
            padding: getComputedStyle(document.documentElement).paddingTop,
            chromeApi: typeof window.__guidebot_chrome,
            cursorApi: typeof window.__guidebot_cursor,
            slideApi: typeof window.__guidebot_slide,
        })"""
    )
    assert leaked == {
        "legacyBar": 0,
        "shellBar": 0,
        "cursor": 0,
        "padding": "0px",
        "chromeApi": "undefined",
        "cursorApi": "undefined",
        "slideApi": "undefined",
    }


class _FramingProtectedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/start":
            # A plain redirect. Because a route-fulfilled 3xx is blocked in a
            # subframe, install_framing follows the chain inside fetch and
            # fulfills the final document at the entry URL — the site loads but
            # frame.url stays at /start.
            self.send_response(301)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        # /final — framing-protected 200 that must still load inside the iframe
        body = b"<h1 id='ok'>final document</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; script-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the test server
        pass


@pytest.fixture
def framing_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FramingProtectedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


async def test_header_stripping_loads_framing_protected_site_and_pill_tracks_frame_url(
    browser, framing_server
) -> None:
    page, site_frame, chrome = await _setup_shell(
        browser, ChromeConfig(enabled=True, height=HEIGHT)
    )

    # A document served with X-Frame-Options: DENY and frame-ancestors 'none'
    # still loads inside the shell iframe once install_framing strips them.
    await site_frame.goto(f"{framing_server}/final")
    assert site_frame.url == f"{framing_server}/final"
    assert await site_frame.locator("#ok").count() == 1

    # The pill reflects the URL sourced from the site frame.
    await chrome.set_url_shell(page, site_frame.url)
    pill_text = await page.evaluate(
        """() => document.querySelector("[data-guidebot-shell-bar]")
            .shadowRoot.querySelector("[data-url]").textContent"""
    )
    assert pill_text == f"{framing_server}/final"


async def test_redirecting_site_loads_in_iframe_at_entry_url(browser, framing_server) -> None:
    # A site that redirects on its entry URL still loads in the shell iframe:
    # fetch follows the 301 to the framing-protected /final document (headers
    # stripped) and fulfills it at /start. The frame commits at the entry URL,
    # so frame.url stays /start — the documented redirect tradeoff.
    _, site_frame, _ = await _setup_shell(browser, ChromeConfig(enabled=True, height=HEIGHT))
    await site_frame.goto(f"{framing_server}/start")
    assert await site_frame.locator("#ok").count() == 1  # final document rendered
    assert site_frame.url == f"{framing_server}/start"  # pill shows the navigated URL
