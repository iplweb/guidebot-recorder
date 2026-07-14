import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_click_executes_and_moves_cursor(page):
    overlay = Overlay()
    await page.set_content("<button onclick=\"this.textContent='clicked'\">Zaloguj</button>")
    await overlay.install(page)
    rec = Recorder(page, overlay)

    await rec.click(RoleTarget(role="button", name="Zaloguj"))

    assert await page.locator("button").text_content() == "clicked"
    # kursor przesunięty w okolice środka przycisku (nie pozostał w (0,0))
    assert overlay.pos != (0.0, 0.0)


async def test_enter_text_fills(page):
    await page.set_content('<label for="e">E-mail</label><input id="e">')
    rec = Recorder(page, overlay=None)  # compile-mode: bez overlay

    await rec.enter_text(RoleTarget(role="textbox", name="E-mail"), "user@x.pl")

    assert await page.locator("#e").input_value() == "user@x.pl"


async def test_apply_readiness_none_is_noop(page):
    await page.set_content("<p>ok</p>")
    rec = Recorder(page, overlay=None)
    await rec.apply_readiness("none")  # nie rzuca
