"""`Recorder.highlight` — zakreśla cel i niczego na stronie nie dotyka."""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.config import CursorConfig, HighlightConfig, Viewport
from guidebot_recorder.models.scenario import Highlight
from guidebot_recorder.models.target import TestidTarget as ByTestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder

FAST = HighlightConfig(loops=1, hold=0.0)


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page(viewport={"width": 800, "height": 600})
        yield pg
        await browser.close()


async def _recorder(page) -> tuple[Recorder, Overlay]:
    overlay = Overlay(CursorConfig(), Viewport(width=800, height=600))
    await overlay.install(page)
    return Recorder(page, overlay), overlay


async def test_highlight_leaves_the_page_untouched(page):
    """Zakreślanie nie może niczego kliknąć — na tym polega cała komenda."""

    await page.set_content(
        '<button data-testid="cel" onclick="this.textContent=\'kliknięty\'"'
        ' style="margin:200px">Zapisz</button>'
    )
    rec, _ = await _recorder(page)

    await rec.highlight(ByTestidTarget(testid="cel"), Highlight(what="przycisk").resolved(FAST))

    assert await page.locator("button").text_content() == "Zapisz"


async def test_highlight_parks_the_cursor_on_the_ellipse_entry_point(page):
    await page.set_content(
        '<div data-testid="cel" style="margin:200px;width:200px;height:60px">Wyniki</div>'
    )
    rec, overlay = await _recorder(page)

    await rec.highlight(ByTestidTarget(testid="cel"), Highlight(what="wyniki").resolved(FAST))

    box = await page.locator('[data-testid="cel"]').bounding_box()
    centre_x = box["x"] + box["width"] / 2
    # prawy skraj elipsy leży dalej niż środek elementu, ale wciąż w kadrze
    assert overlay.pos[0] > centre_x
    assert overlay.pos[0] <= 800.0


async def test_highlight_survives_an_element_without_a_box(page):
    """Element bez prostokąta to brak animacji, nie wywrotka przebiegu."""

    await page.set_content('<span data-testid="cel" style="display:contents">nic</span>')
    rec, _ = await _recorder(page)

    await rec.highlight(ByTestidTarget(testid="cel"), Highlight(what="nic").resolved(FAST))


async def test_highlight_keeps_the_ellipse_inside_the_viewport(page):
    """Szeroki element: elipsa jest przycięta do kadru, kursor nie ucieka z ekranu."""

    await page.set_content(
        '<div data-testid="cel" style="margin:20px;width:760px;height:40px">szeroka tabela</div>'
    )
    rec, overlay = await _recorder(page)

    await rec.highlight(ByTestidTarget(testid="cel"), Highlight(what="tabela").resolved(FAST))

    assert 0.0 <= overlay.pos[0] <= 800.0
    assert 0.0 <= overlay.pos[1] <= 600.0
