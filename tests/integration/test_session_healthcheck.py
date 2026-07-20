"""Integration: the login health-check against a real cookie-gated HTTP server.

A tiny server renders the authenticated text ("Wyloguj Jan") only when a session
cookie is present, and a login page (without it) otherwise. This exercises
``check_logged_in`` end to end on real chromium: a seeded session passes, an
empty one fails (by polling to a short timeout), and no page text ever escapes.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.recorder.session import check_logged_in

pytestmark = pytest.mark.integration

# The dashboard's authenticated body carries a real username; a leak of this
# string into any log/error would be a PII regression.
USERNAME = "Jan Kowalski"
LOGGED_IN_TEXT = "Wyloguj"

_LOGIN_PAGE = (
    "<!doctype html><html lang='pl'><body>"
    "<h1>Logowanie</h1><p>Nie zalogowano</p>"
    "</body></html>"
)


def _dashboard_page() -> str:
    return (
        "<!doctype html><html lang='pl'><body>"
        f"<h1>Panel</h1><p>{LOGGED_IN_TEXT} {USERNAME}</p>"
        "</body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        cookie = self.headers.get("Cookie", "")
        body = _dashboard_page() if "session=" in cookie else _LOGIN_PAGE
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence the default stderr logging
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("localhost", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        thread.join()


@pytest.fixture
async def browser() -> AsyncIterator:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


def _seeded_state(value: str = "live") -> dict:
    return {
        "cookies": [
            {
                "name": "session",
                "value": value,
                "domain": "localhost",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


_VIEWPORT = {"width": 800, "height": 600}


async def test_check_passes_with_seeded_cookie(server, browser) -> None:
    ok = await check_logged_in(
        browser,
        _seeded_state(),
        goto_url=server,
        contains_text=LOGGED_IN_TEXT,
        locale="pl-PL",
        viewport=_VIEWPORT,
        timeout=5,
    )
    assert ok is True


async def test_check_fails_without_cookie(server, browser) -> None:
    ok = await check_logged_in(
        browser,
        {"cookies": [], "origins": []},
        goto_url=server,
        contains_text=LOGGED_IN_TEXT,
        locale="pl-PL",
        viewport=_VIEWPORT,
        timeout=2,  # short: the text will never appear, so this is the poll window
    )
    assert ok is False


async def test_check_never_raises_or_leaks_page_text(server, browser) -> None:
    """Even the failing path returns a plain bool; it must never surface body text.

    We assert the function does not raise (so no message can carry page text) and
    that its boolean verdict is all that is observable.
    """

    # Logged-in page, but the configured text is absent → poll to timeout → False,
    # with no exception and therefore no channel for the username to escape.
    try:
        verdict = await check_logged_in(
            browser,
            _seeded_state(),
            goto_url=server,
            contains_text="TEKST-KTÓREGO-NIE-MA",
            locale="pl-PL",
            viewport=_VIEWPORT,
            timeout=2,
        )
    except Exception as exc:  # pragma: no cover - defensive
        assert USERNAME not in str(exc)
        raise
    assert verdict is False
