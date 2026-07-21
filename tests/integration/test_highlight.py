"""E2E: `highlight` przechodzi compile → guide, zaznacza cel i niczego nie klika."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "highlight.html"

SCENARIO_TEMPLATE = """\
config:
  title: Raport
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  highlight: {{loops: 1, hold: 0.0}}
steps:
  - navigate: "{url}"
  - highlight: {{what: "tabela z wynikami", color: "#22c55e"}}
    say: "Tutaj pojawiają się wyniki."
  - highlight: "przycisk Zapisz"
"""


class HighlightMockReasoner:
    """Rozwiązuje oba cele scenariusza; zwrócona akcja i tak nie decyduje.

    Dla kroku innego niż `teach` o akcji decyduje plik scenariusza
    (``action_for``), więc mock celowo zwraca tu „hover" — gdyby ta wartość
    przeciekała do sidecara, test by to pokazał.
    """

    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "tabela" in instruction:
            return ReasonerResult("hover", RoleTarget(role="table", name="Wyniki"))
        if "Zapisz" in instruction:
            return ReasonerResult("hover", RoleTarget(role="button", name="Zapisz"))
        raise AssertionError(f"nieoczekiwana instrukcja: {instruction!r}")


async def test_highlight_compiles_and_marks_the_target_without_touching_it(tmp_path):
    from guidebot_recorder.guide.capture import capture_pages
    from guidebot_recorder.overlay.overlay import Overlay
    from guidebot_recorder.recorder.recorder import Recorder
    from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
    from guidebot_recorder.scenario.loader import load_scenario

    url = FIXTURE.resolve().as_uri()
    path = tmp_path / "raport.scenario.yaml"
    path.write_text(SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            compile_page = await browser.new_page()
            await run_compile(path, compile_page, HighlightMockReasoner())
            await compile_page.context.close()

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
                page.set_default_timeout(10_000)
                recorder = Recorder(page, overlay)

                pages = await capture_pages(
                    scenario,
                    compiled,
                    page,
                    recorder,
                    tmp_path / "shots",
                    timeout=10.0,
                )
                clicked = await page.locator("#zapisz").text_content()
            finally:
                await context.close()
        finally:
            await browser.close()

    # sidecar: akcja pochodzi ze scenariusza, nie z odpowiedzi reasonera
    assert [a.action for a in compiled.actions if a is not None] == ["highlight", "highlight"]

    # zaznaczenie trafiło na stronę przewodnika jako elipsa we własnym kolorze
    marks = [ann for p in pages for ann in p.annotations if ann.kind == "highlight"]
    assert len(marks) == 2
    assert marks[0].color == "#22c55e"
    assert marks[0].rx > 0 and marks[0].ry > 0

    # …i nic na stronie nie zostało dotknięte
    assert clicked == "Zapisz"


async def test_optional_highlight_of_an_absent_target_is_skipped(tmp_path):
    """Nieobecny cel z `optional: true` pomija krok, zamiast wywracać przebieg."""

    from guidebot_recorder.guide.guide import run_guide

    url = FIXTURE.resolve().as_uri()
    path = tmp_path / "raport-opcjonalny.scenario.yaml"
    path.write_text(
        SCENARIO_TEMPLATE.format(url=url)
        + '  - highlight: "baner zgody na ciasteczka"\n    optional: true\n',
        encoding="utf-8",
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            compile_page = await browser.new_page()

            class WithAbsentBanner(HighlightMockReasoner):
                async def resolve(self, instruction, candidates):
                    if "baner" in instruction:
                        return ReasonerError("no_action", "nie ma takiego elementu")
                    return await super().resolve(instruction, candidates)

            await run_compile(path, compile_page, WithAbsentBanner())
            await compile_page.context.close()

            out = tmp_path / "raport.pdf"
            count = await run_guide(path, out, browser, timeout=10.0)
        finally:
            await browser.close()

    assert out.exists() and out.stat().st_size > 0
    assert count >= 2
