"""Pomiary layoutu przewodnika w prawdziwym Chromium.

`test_layout.py` sprawdza, co renderer wypisuje; tutaj sprawdzamy, co z tego
wychodzi po ułożeniu strony — bo obie wady, które ten plik pilnuje, były
niewidoczne w HTML-u i widoczne dopiero w geometrii:

* narracja obcinana przy prawej krawędzi (kolumny sumowały się do 100%, a `gap`
  dochodził ponad to),
* scenki, które nie mieszczą się po dwie na kartce.

Wzorzec (fixture `page` na headless Chromium) jak w `tests/unit/slide/test_slide.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.guide.layout import render_html
from guidebot_recorder.guide.model import GuidePage

# A4 poziomo w pikselach CSS przy 96 dpi (297×210 mm), pomniejszone o margines
# `@page` 12mm z obu stron — czyli obszar, na którym naprawdę leżą scenki.
_A4_LANDSCAPE_CONTENT = {"width": 1032, "height": 703}


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        pg = await browser.new_page(viewport=_A4_LANDSCAPE_CONTENT)
        try:
            yield pg
        finally:
            await browser.close()


def _shot(text: str) -> GuidePage:
    return GuidePage(
        kind="step",
        screenshot=Path("/tmp/shot.png"),
        text=text,
        heading=None,
        annotations=[],
        screenshot_size=(1280, 720),
        canvas_size=(1280, 720),
    )


async def test_no_scene_child_reaches_past_its_own_row(page: Page) -> None:
    """Żadna kolumna scenki nie wystaje poza scenkę — inaczej druk ucina tekst.

    Regresja wprost: przy `grid-template-columns: 62% 38%` plus `gap: 6mm` tory
    zajmowały 100% szerokości PLUS odstęp, więc prawa kolumna wychodziła poza
    papier o całą szerokość `gap` (zmierzone: 23 px przy 1056 px strony), a
    Chromium ucinało to, co przekroczyło krawędź — po 2–3 znaki z każdego wiersza
    narracji, bez śladu w HTML-u i bez ostrzeżenia.
    """

    długa = "Ze względów bezpieczeństwa hasło nie jest widoczne na ekranie. " * 3
    await page.set_content(render_html([_shot(długa), _shot(długa)], title="t"))

    przekroczenia = await page.evaluate(
        """() => {
          const out = [];
          for (const scene of document.querySelectorAll('.scene')) {
            const row = scene.getBoundingClientRect();
            for (const child of scene.children) {
              const box = child.getBoundingClientRect();
              // Zaokrąglenia subpikselowe są nieuniknione; 1 px tolerancji.
              if (box.right > row.right + 1) {
                out.push({cls: child.className, o: Math.round(box.right - row.right)});
              }
            }
          }
          return out;
        }"""
    )
    assert przekroczenia == []


async def test_body_does_not_scroll_sideways(page: Page) -> None:
    """Dokument nie może być szerszy niż kartka — to ten sam błąd, widziany z góry."""

    await page.set_content(render_html([_shot("krótko"), _shot("krótko")], title="t"))
    assert await page.evaluate("() => document.documentElement.scrollWidth") <= (
        _A4_LANDSCAPE_CONTENT["width"] + 1
    )


async def test_two_short_scenes_fit_on_one_sheet_and_a_third_does_not(page: Page) -> None:
    """Dwie scenki na kartkę — to jest cała umowa tego layoutu.

    Mierzone wysokością: dwie muszą zmieścić się w obszarze druku, a trzecia już
    nie, bo inaczej `min-height` przestałby wymuszać podział i na kartce
    lądowałyby trzy ściśnięte scenki albo jedna z pustym polem pod spodem.
    """

    await page.set_content(render_html([_shot("krótko")] * 3, title="t"))
    wysokości = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.scene'),
                            e => e.getBoundingClientRect().height)"""
    )
    assert len(wysokości) == 3
    assert sum(wysokości[:2]) <= _A4_LANDSCAPE_CONTENT["height"]
    assert sum(wysokości) > _A4_LANDSCAPE_CONTENT["height"]


async def test_still_takes_between_40_and_50_percent_of_the_sheet_width(page: Page) -> None:
    """Szerokość kadru trzyma się przedziału, o który prosił autor przewodników.

    Wysokość wychodzi z proporcji 16:9 i jest przez to mniejsza (~34%) — przy
    kadrze 1280×720 na kartce A4 poziomo nie da się mieć naraz 40–50% szerokości
    i 40–50% wysokości, a to szerokość decyduje o czytelności zrzutu.
    """

    await page.set_content(render_html([_shot("krótko")], title="t"))
    wymiary = await page.evaluate(
        """() => {
          const shot = document.querySelector('.shot').getBoundingClientRect();
          const scene = document.querySelector('.scene').getBoundingClientRect();
          return {szerokosc: shot.width / scene.width, wysokosc: shot.height};
        }"""
    )
    assert 0.40 <= wymiary["szerokosc"] <= 0.50
    # Kadr musi zostać niższy niż połowa obszaru druku, inaczej dwie scenki
    # przestaną się mieścić na kartce — to jest granica, o którą naprawdę chodzi.
    assert wymiary["wysokosc"] < _A4_LANDSCAPE_CONTENT["height"] / 2
