"""Integration: replay a compiled setup scenario into a cached session.

The load-bearing proof for Phase A: a compiled setup sidecar is replayed through
the compile path with :class:`RaisingReasoner` (NO reasoner calls) and drives the
frozen targets against a real page whose button click sets a session cookie. The
captured ``storage_state`` must carry that cookie — i.e. replay genuinely
establishes a session with zero LLM involvement.

Rather than hand-authoring an ``Identity``/``ancestry_digest`` (fragile), the
setup is first compiled with a deterministic mock reasoner; the replay then runs
against that fresh, valid sidecar with the raising reasoner — proving the exact
same property (frozen-target replay, no inference) more robustly.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.session import (
    SetupNeedsCompile,
    SetupSessionError,
    ensure_session,
    replay_setup,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult

pytestmark = pytest.mark.integration

USERNAME = "Jan Kowalski"
LOGIN_BUTTON = "Zaloguj się"

_LOGIN_PAGE = (
    "<!doctype html><html lang='pl'><body>"
    "<h1>Logowanie</h1>"
    "<button onclick=\"document.cookie='session=live; path=/'\">"
    f"{LOGIN_BUTTON}</button>"
    "</body></html>"
)


def _dashboard_page() -> str:
    return (
        "<!doctype html><html lang='pl'><body>"
        f"<h1>Panel</h1><p>Wyloguj {USERNAME}</p>"
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

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("localhost", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{httpd.server_address[1]}"
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


class _LoginReasoner:
    """Deterministic: resolves the single teach step to a click on the button."""

    async def resolve(self, instruction, candidates):
        return ReasonerResult("click", RoleTarget(role="button", name=LOGIN_BUTTON, exact=True))


_SETUP_TEMPLATE = """\
config:
  title: Login
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
{extra}steps:
  - navigate: "/"
  - teach: "kliknij {button}"
"""

_TARGET_TEMPLATE = """\
config:
  title: Target
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
  setup: login.scenario.yaml
steps:
  - navigate: "/"
"""


def _write_setup(tmp_path: Path, base_url: str, *, verify: str | None = None) -> Path:
    extra = ""
    if verify is not None:
        extra = f"  verifyUserLoggedIn: {{containsText: '{verify}', timeout: 2}}\n"
    path = tmp_path / "login.scenario.yaml"
    path.write_text(
        _SETUP_TEMPLATE.format(base_url=base_url, button=LOGIN_BUTTON, extra=extra),
        encoding="utf-8",
    )
    return path


def _cookie_names(state: dict) -> set[str]:
    return {c.get("name") for c in state.get("cookies", [])}


async def test_replay_of_compiled_setup_yields_session_cookie(server, browser, tmp_path) -> None:
    setup = _write_setup(tmp_path, server)

    # 1) Compile the setup with a deterministic reasoner (freezes the click target).
    page = await browser.new_page()
    await run_compile(setup, page, _LoginReasoner(), {}, selects=None)
    await page.context.close()

    # 2) Replay it with the RAISING reasoner: frozen targets only, no inference.
    state = await replay_setup(browser, setup, {}, timeout=15)

    assert "session" in _cookie_names(state), state.get("cookies")


async def test_replay_of_uncompiled_setup_raises_setup_needs_compile(
    server, browser, tmp_path
) -> None:
    """Fallback guarantee: with no valid sidecar, replay fails loudly (no LLM)."""

    setup = _write_setup(tmp_path, server)
    # Never compiled → the teach step is not reuse-valid → RaisingReasoner fires.
    with pytest.raises(SetupNeedsCompile, match="compile"):
        await replay_setup(browser, setup, {}, timeout=15)


async def test_ensure_session_double_failure_diagnostic_hides_page_text(
    server, browser, tmp_path
) -> None:
    """A real replay that logs in, but whose configured verify text is absent,
    raises the text-not-found diagnostic — and never leaks the page's username."""

    # verify text that will NOT be present on the dashboard → health-check fails
    setup = _write_setup(tmp_path, server, verify="TEKST-KTÓREGO-NIE-MA")
    page = await browser.new_page()
    await run_compile(setup, page, _LoginReasoner(), {}, selects=None)
    await page.context.close()

    target = tmp_path / "target.scenario.yaml"
    target.write_text(_TARGET_TEMPLATE.format(base_url=server), encoding="utf-8")

    with pytest.raises(SetupSessionError) as excinfo:
        await ensure_session(
            browser,
            target,
            tmp_path / "sessions",
            {},
            timeout=15,
            warn=lambda m: None,
        )
    message = str(excinfo.value)
    # The replay produced a cookie (non-empty), so this is the text-not-found
    # branch, not the sessionStorage/IndexedDB one.
    assert "verifyUserLoggedIn" in message or "--headed" in message
    # Page text (the username) must never appear in the error.
    assert USERNAME not in message
    assert "Wyloguj" not in message
