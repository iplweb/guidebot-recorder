"""Integration (Phase B2): the setup session is wired into BOTH compile and render.

The load-bearing insight of pre-recording setup: when a target scenario declares
``config.setup``, its login steps are *removed*, so the target must be compiled
AND rendered against an already-logged-in context. This module proves the wiring
end to end against a local HTTP server whose ``/app`` route only exposes the
target's element when an auth cookie is present.

- A SETUP scenario logs in (navigate ``/login``, fill creds, click submit) and is
  compiled with a deterministic mock reasoner so it is reuse-valid for replay.
- A TARGET scenario declares ``config.setup`` and has NO login steps — it merely
  navigates to ``/app`` and acts on the logged-in-only button.

TEST A: ``run_compile_in_browser`` of the target succeeds — proving
``ensure_session`` seeded the compile context so the logged-in-only element
resolves; the target sidecar carries a non-null click action for that step. A
companion check confirms that without the seeding (cfg.setup removed) the same
target lands logged-out and the element cannot be resolved.

TEST B: ``run_render`` of the target succeeds and produces the mp4. Because the
target has no login steps, the login is absent from the film by construction.

TEST C (guard): a target whose setup scenario is NOT compiled surfaces
``SetupNeedsCompile`` from ``ensure_session``.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import LabelTarget, RoleTarget
from guidebot_recorder.recorder.compile import run_compile, run_compile_in_browser
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.recorder.session import SetupNeedsCompile
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.video.mux.probe import probe_duration

pytestmark = pytest.mark.integration

_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe niedostępne",
)

PANEL_BUTTON = "Otwórz panel"
LOGIN_BUTTON = "Zaloguj się"

_LOGIN_PAGE = (
    "<!doctype html><html lang='pl'><body>"
    "<h1>Logowanie</h1>"
    "<form onsubmit='return false'>"
    "<label for='u'>Login</label><input id='u' type='text'>"
    "<label for='p'>Hasło</label><input id='p' type='password'>"
    "</form>"
    "<button onclick=\"document.cookie='auth=ok; path=/'; location.href='/app'\">"
    f"{LOGIN_BUTTON}</button>"
    "</body></html>"
)

_PANEL_PAGE = (
    "<!doctype html><html lang='pl'><body>"
    "<h1>Panel</h1>"
    f"<button>{PANEL_BUTTON}</button>"
    "</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        logged_in = "auth=" in self.headers.get("Cookie", "")
        if path.startswith("/app"):
            if not logged_in:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            body = _PANEL_PAGE
        else:
            body = _LOGIN_PAGE
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


class SetupReasoner:
    """Deterministic: fills the two credential fields, clicks the submit button."""

    async def resolve(self, instruction, candidates):
        if instruction == "pole login":
            return ReasonerResult("type", LabelTarget(label="Login"))
        if instruction == "pole hasło":
            return ReasonerResult("type", LabelTarget(label="Hasło"))
        return ReasonerResult("click", RoleTarget(role="button", name=LOGIN_BUTTON, exact=True))


class TargetReasoner:
    """Answers only from the live candidates, so a logged-out page yields absence."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, instruction, candidates):
        self.calls.append(instruction)
        names = {(c.role, c.name) for c in candidates}
        if ("button", PANEL_BUTTON) not in names:
            return ReasonerError("no_handle", "brak przycisku panelu — wylogowany")
        return ReasonerResult("click", RoleTarget(role="button", name=PANEL_BUTTON, exact=True))


class FakeTts:
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "0.3",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return 0.3


_SETUP_TEMPLATE = """\
config:
  title: Login
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
  verifyUserLoggedIn: {{containsText: 'Panel', url: '/app', timeout: 3}}
steps:
  - navigate: "/login"
  - enterText: {{into: "pole login", text: "jan"}}
  - enterText: {{into: "pole hasło", text: "sekret"}}
  - teach: "kliknij Zaloguj"
"""

_TARGET_TEMPLATE = """\
config:
  title: Target
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
{setup_line}steps:
  - navigate: "/app"
  - teach: "kliknij {button}"
"""


def _write_setup(dir_: Path, base_url: str) -> Path:
    path = dir_ / "login.scenario.yaml"
    path.write_text(_SETUP_TEMPLATE.format(base_url=base_url), encoding="utf-8")
    return path


def _write_target(dir_: Path, base_url: str, *, with_setup: bool) -> Path:
    setup_line = "  setup: login.scenario.yaml\n" if with_setup else ""
    path = dir_ / "target.scenario.yaml"
    path.write_text(
        _TARGET_TEMPLATE.format(base_url=base_url, setup_line=setup_line, button=PANEL_BUTTON),
        encoding="utf-8",
    )
    return path


async def _compile_setup(browser, setup: Path) -> None:
    page = await browser.new_page()
    await run_compile(setup, page, SetupReasoner(), {}, selects=None)
    await page.context.close()


# --------------------------------------------------------------------------- #
# TEST A — the load-bearing compile test
# --------------------------------------------------------------------------- #


async def test_target_compile_is_seeded_with_the_setup_session(
    server, browser, tmp_path, monkeypatch
) -> None:
    """A target whose step-1 element exists only when logged in compiles because
    ``ensure_session`` seeded the compile context (spec: review §1)."""

    monkeypatch.chdir(tmp_path)  # `.guidebot/sessions` lands under tmp_path
    setup = _write_setup(tmp_path, server)
    await _compile_setup(browser, setup)

    target = _write_target(tmp_path, server, with_setup=True)
    reasoner = TargetReasoner()
    await run_compile_in_browser(target, browser, reasoner, env={}, timeout=15)

    # flat indices: 0 navigate, 1 teach → the logged-in-only button.
    compiled = load_compiled(compiled_path(target))
    action = compiled.actions[1]
    assert isinstance(action, CachedAction), compiled.actions
    assert action.action == "click"
    assert PANEL_BUTTON in reasoner.calls[-1]


async def test_target_compile_without_setup_lands_logged_out(
    server, browser, tmp_path, monkeypatch
) -> None:
    """Companion falsifier: drop ``config.setup`` and the same target lands on the
    logged-out ``/login`` page — the button is absent, so compile fails loudly.

    This proves the success above is due to the seeding, not to the button being
    reachable anyway.
    """

    monkeypatch.chdir(tmp_path)
    _write_setup(tmp_path, server)
    target = _write_target(tmp_path, server, with_setup=False)
    with pytest.raises(RuntimeError):
        await run_compile_in_browser(target, browser, TargetReasoner(), env={}, timeout=15)


# --------------------------------------------------------------------------- #
# TEST B — render receives the session; login is absent from the film
# --------------------------------------------------------------------------- #


@_ffmpeg
@pytest.mark.ffmpeg
async def test_target_render_is_seeded_and_produces_video(
    server, browser, tmp_path, monkeypatch
) -> None:
    """The target renders successfully against the seeded (logged-in) context and
    produces a playable mp4. The target has no login steps, so the login page can
    never appear in the recording by construction."""

    monkeypatch.chdir(tmp_path)
    setup = _write_setup(tmp_path, server)
    await _compile_setup(browser, setup)

    target = _write_target(tmp_path, server, with_setup=True)
    await run_compile_in_browser(target, browser, TargetReasoner(), env={}, timeout=15)

    out = tmp_path / "target.mp4"
    await run_render(target, out, FakeTts(), tmp_path / "cache", browser, env={}, timeout=15)

    assert out.exists()
    assert probe_duration(out) > 0


# --------------------------------------------------------------------------- #
# TEST C — guard: setup scenario not compiled → SetupNeedsCompile
# --------------------------------------------------------------------------- #


async def test_uncompiled_setup_surfaces_setup_needs_compile(
    server, browser, tmp_path, monkeypatch
) -> None:
    """When the referenced setup scenario was never compiled, seeding the target
    compile fails loudly via ``ensure_session`` → ``replay_setup`` (no LLM)."""

    monkeypatch.chdir(tmp_path)
    _write_setup(tmp_path, server)  # written, but deliberately NOT compiled
    target = _write_target(tmp_path, server, with_setup=True)
    with pytest.raises(SetupNeedsCompile):
        await run_compile_in_browser(target, browser, TargetReasoner(), env={}, timeout=15)
