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
SELECT_SCROLL_FIXTURE = Path(__file__).parent / "fixtures" / "select_scroll.html"
SELECT_REVEAL_FIXTURE = Path(__file__).parent / "fixtures" / "select_reveal.html"

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


SELECT_SCROLL_SCENARIO_TEMPLATE = """\
config:
  title: Raport
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Pokażę, jak wybrać zakres dat i przewinąć do podsumowania."
  - navigate: "{url}"
  - select: {{from: "zakres dat", option: "Cały okres"}}
    say: "Wybieram cały okres."
  - scroll: down
  - scroll: bottom
    say: "Przewijam do podsumowania na dole strony."
"""


#: Only `navigate` -> `select` -> `scroll: down` (no text on the scroll step),
#: so `capture_pages`'s post-run state is exactly the effect of these two
#: live-DOM actions with nothing downstream to overwrite it.
DIRECT_CAPTURE_SCENARIO_TEMPLATE = """\
config:
  title: Raport
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - navigate: "{url}"
  - select: {{from: "zakres dat", option: "Cały okres"}}
    say: "Wybieram cały okres."
  - scroll: down
"""


SELECT_REVEAL_SCENARIO_TEMPLATE = """\
config:
  title: Filtry
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - navigate: "{url}"
  - select: {{from: "tryb wyszukiwania", option: "Zaawansowany"}}
    say: "Przełączam na tryb zaawansowany."
  - enterText: {{into: "Rok od", text: "2020"}}
    say: "Wpisuję rok początkowy."
"""


class MockReasoner:
    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "Zaloguj" in instruction:
            return ReasonerResult("click", RoleTarget(role="button", name="Zaloguj"))
        return ReasonerResult("type", RoleTarget(role="textbox", name="E-mail"))


class SelectScrollMockReasoner:
    """Resolves the one target this fixture's scenario needs: the native select."""

    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "zakres" in instruction:
            return ReasonerResult("select", RoleTarget(role="combobox", name="Zakres dat"))
        raise AssertionError(f"unexpected instruction for this fixture: {instruction!r}")


class SelectRevealMockReasoner:
    """Resolves the two targets `select_reveal.html` needs: the mode selector,
    then the "Rok od" field that only exists in the DOM once "Zaawansowany"
    has actually been selected.
    """

    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        lowered = instruction.lower()
        if "tryb" in lowered:
            return ReasonerResult("select", RoleTarget(role="combobox", name="Tryb wyszukiwania"))
        if "rok od" in lowered:
            return ReasonerResult("type", RoleTarget(role="textbox", name="Rok od"))
        raise AssertionError(f"unexpected instruction for this fixture: {instruction!r}")


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


async def test_guide_handles_select_and_scroll(tmp_path):
    """`scroll` runs every time but only produces a PDF page when the step
    also carries text.

    Page count, counted by hand against `SELECT_SCROLL_SCENARIO_TEMPLATE`:
      1. leading `say`                          -> text page
      2. `navigate`                             -> navigate page
      3. `select` (+ `say`)                     -> action page (always, regardless of text)
      4. `scroll: down` (no text)                -> NO page
      5. `scroll: bottom` (+ `say`)              -> step page
    Total: 4 pages. If `scroll` step 4 wrongly produced a page, this would be 5.

    NOTE: this exact page count is *not*, by itself, proof that `select`
    executes — a `select` step that silently no-ops still lands on the
    `classify()` fallback and produces the same one text/action page, so the
    total stays 4 either way. `select` actually driving the live DOM is
    covered separately by `test_guide_select_actually_executes_and_unlocks_next_step`
    (a dependent later step only succeeds if the choice really changed the
    page) and by `test_capture_pages_executes_select_and_scroll_on_the_live_page`
    (asserts the live `<select>` value and the page's `selected` annotation
    directly).
    """

    from guidebot_recorder.guide.guide import run_guide

    url = SELECT_SCROLL_FIXTURE.resolve().as_uri()
    path = tmp_path / "select-scroll.scenario.yaml"
    path.write_text(SELECT_SCROLL_SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # --- compile (offline reasoner, no LLM) ---
            page = await browser.new_page()
            reasoner = SelectScrollMockReasoner()
            await run_compile(path, page, reasoner, selects=None)
            await page.context.close()

            # --- guide ---
            out = tmp_path / "select-scroll.pdf"
            count = await run_guide(path, out, browser, timeout=10.0)
        finally:
            await browser.close()

    assert out.exists() and out.stat().st_size > 0
    assert count == 4


async def test_guide_select_actually_executes_and_unlocks_next_step(tmp_path):
    """`select` must really change the live DOM, not just render a page.

    `select_reveal.html` starts with a "Rok od" field hidden (`display: none`)
    until its `<select>` is switched to "Zaawansowany". The scenario's next
    step, `enterText: {into: "Rok od", ...}`, only has anything to type into
    once that switch actually happened live in the browser.

    This reproduces the real-world bug this branch fixes: an unresolved
    `select` step used to fall through `classify()`'s fallback and render a
    page without ever calling `locator.select_option`, silently leaving the
    DOM one step behind everything downstream. `compile` performs the select
    for real (see `recorder/compile.py::_compile_step`), so it always reveals
    the field and always succeeds — this test's discriminating power is
    entirely in the `guide` phase:

    - pre-fix `guide`: `select` never executes -> "Rok od" stays hidden ->
      `reuse_failure` on the `enterText` step returns `not_visible` ->
      `capture_pages` raises `GuideError` -> this test errors out.
    - post-fix `guide`: `select` executes for real -> the field is visible ->
      `enterText` succeeds -> this test passes.
    """

    from guidebot_recorder.guide.guide import run_guide

    url = SELECT_REVEAL_FIXTURE.resolve().as_uri()
    path = tmp_path / "select-reveal.scenario.yaml"
    path.write_text(SELECT_REVEAL_SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # --- compile (offline reasoner, no LLM) ---
            page = await browser.new_page()
            reasoner = SelectRevealMockReasoner()
            await run_compile(path, page, reasoner, selects=None)
            await page.context.close()

            # --- guide ---
            out = tmp_path / "select-reveal.pdf"
            count = await run_guide(path, out, browser, timeout=10.0)
        finally:
            await browser.close()

    assert out.exists() and out.stat().st_size > 0
    # navigate + select (action page) + enterText (action page)
    assert count == 3


async def test_capture_pages_executes_select_and_scroll_on_the_live_page(tmp_path):
    """Drive `capture_pages` directly against a live `Page` (bypassing
    `run_guide`, which only returns a page count and closes the context, so it
    cannot show any live-DOM effect).

    Builds the same context/overlay/`Recorder` recipe `guide.py::run_guide`
    uses, minus the chrome shell, then asserts directly on the live page and
    on the returned `GuidePage` for the `select` step:

    - `window.scrollY` is > 0 after a `scroll: down` step that carries no
      text — proving `scroll` always executes, even for a step that produces
      no PDF page (pre-fix, `scroll` never ran at all: neither text-bearing
      nor bare `scroll` steps called `recorder.scroll()`, so this would stay
      `0`);
    - the live `<select>`'s value is the option the scenario chose (pre-fix,
      `select` never called `locator.select_option`, so it would still be the
      HTML default, `"month"`, not `"all"`);
    - the `GuidePage` produced for the `select` step is a `kind == "step"`
      page with a non-empty `screenshot` and a `selected` annotation (pre-fix,
      an unresolved `select` fell through `classify()`'s fallback into
      `kind == "text"` with `screenshot=None` and no annotations at all).
    """

    from guidebot_recorder.guide.capture import capture_pages
    from guidebot_recorder.overlay.overlay import Overlay
    from guidebot_recorder.recorder.recorder import Recorder
    from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
    from guidebot_recorder.scenario.loader import load_scenario

    url = SELECT_SCROLL_FIXTURE.resolve().as_uri()
    path = tmp_path / "select-scroll-direct.scenario.yaml"
    path.write_text(DIRECT_CAPTURE_SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # --- compile (offline reasoner, no LLM) ---
            compile_page = await browser.new_page()
            reasoner = SelectScrollMockReasoner()
            await run_compile(path, compile_page, reasoner, selects=None)
            await compile_page.context.close()

            # --- guide's own context/overlay/Recorder recipe, driven directly ---
            scenario = load_scenario(path, None)
            compiled = load_compiled(compiled_path(path))
            cfg = scenario.config

            context = await browser.new_context(
                viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
                locale=cfg.locale,
            )
            try:
                overlay = Overlay(cfg.cursor, cfg.viewport)
                await overlay.install_context(context)
                page = await context.new_page()
                page.set_default_timeout(10.0 * 1000)
                recorder = Recorder(page, overlay, frame=None, type_delay_ms=None)

                pages = await capture_pages(
                    scenario, compiled, page, recorder, tmp_path / "shots", timeout=10.0
                )

                scroll_y = await page.evaluate("window.scrollY")
                select_value = await page.locator("#range").input_value()
            finally:
                await context.close()
        finally:
            await browser.close()

    assert scroll_y > 0, "scroll: down (no text) must scroll the live page even without a PDF page"
    assert select_value == "all", "select must set the live <select> to the chosen option"

    # navigate, then select (+ say) -> exactly 2 pages; the trailing bare
    # `scroll: down` produces none.
    assert len(pages) == 2
    select_page = pages[1]
    assert select_page.kind == "step"
    assert select_page.screenshot is not None
    assert any(annotation.kind == "selected" for annotation in select_page.annotations)
