"""E2E: compile a tiny scenario, then run `guide` and assert a real PDF.

Mirrors the compile harness from `tests/integration/test_compile_render.py`
(same fixture, same MockReasoner pattern) but drives `run_guide` instead of
`run_render` — no ffmpeg/TTS involved, so this test has no ffmpeg skip guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.resolver.reasoner import ReasonerResult

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "app.html"

SCENARIO_TEMPLATE = """\
config:
  title: Logowanie
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Pokażę, jak się zalogować."
  - navigate: "{url}"
  - teach: "kliknij Zaloguj"
  - enterText: {{into: "pole email", text: "test@example.test"}}
    say: "Wpisuję adres e-mail."
"""


CHROME_SCENARIO_TEMPLATE = """\
config:
  title: Logowanie (chrome)
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
steps:
  - navigate: "{url}"
  - teach: "kliknij Zaloguj"
  - enterText: {{into: "pole email", text: "test@example.test"}}
    say: "Wpisuję adres e-mail."
"""


class MockReasoner:
    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "Zaloguj" in instruction:
            return ReasonerResult("click", RoleTarget(role="button", name="Zaloguj"))
        return ReasonerResult("type", RoleTarget(role="textbox", name="E-mail"))


async def test_guide_produces_pdf_with_expected_pages(tmp_path):
    from guidebot_recorder.guide.guide import run_guide

    url = FIXTURE.resolve().as_uri()
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # --- compile (offline reasoner, no LLM) ---
            page = await browser.new_page()
            reasoner = MockReasoner()
            # The guide's own context installs no select shim, and this
            # fixture has no `select:` step; `selects=None` is the call
            # site saying so out loud, as `run_compile` requires.
            await run_compile(path, page, reasoner, selects=None)
            await page.context.close()

            # --- guide ---
            out = tmp_path / "guide.pdf"
            count = await run_guide(path, out, browser, timeout=10.0)
        finally:
            await browser.close()

    assert out.exists() and out.stat().st_size > 0
    assert count >= 2  # navigate page + at least one action page


async def test_guide_produces_pdf_with_chrome_shell_enabled(tmp_path):
    """Same flow, but `chrome.enabled: true` — exercises the site_frame path."""

    from guidebot_recorder.guide.guide import run_guide

    url = FIXTURE.resolve().as_uri()
    path = tmp_path / "login-chrome.scenario.yaml"
    path.write_text(CHROME_SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            reasoner = MockReasoner()
            await run_compile(path, page, reasoner, selects=None)
            await page.context.close()

            out = tmp_path / "guide-chrome.pdf"
            count = await run_guide(path, out, browser, timeout=10.0)
        finally:
            await browser.close()

    assert out.exists() and out.stat().st_size > 0
    assert count >= 2
