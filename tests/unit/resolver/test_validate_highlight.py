"""Walidacja celu `highlight` — istnienie, unikalność, widoczność i nic ponadto."""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,  # alias: pytest próbuje zbierać `Test*` jako klasę testów
)
from guidebot_recorder.models.target import (
    TextTarget,
)
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    validate_compile_time,
)


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_accepts_a_plain_region_that_is_neither_clickable_nor_editable(page):
    """Zakreślamy obszary — wyłączony, nieinteraktywny kontener to poprawny cel."""

    await page.set_content('<div data-testid="wyniki" aria-disabled="true">Wyniki: 3</div>')

    result = await validate_compile_time(page, ByTestidTarget(testid="wyniki"), "highlight")

    assert isinstance(result, ValidationOk)


async def test_rejects_an_invisible_target(page):
    await page.set_content('<div data-testid="wyniki" style="display:none">Wyniki</div>')

    result = await validate_compile_time(page, ByTestidTarget(testid="wyniki"), "highlight")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_visible"


async def test_rejects_an_ambiguous_target(page):
    await page.set_content("<p>Wyniki</p><p>Wyniki</p>")

    result = await validate_compile_time(page, TextTarget(text="Wyniki", exact=True), "highlight")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_unique"


async def test_rejects_a_missing_target(page):
    await page.set_content("<p>nic tu nie ma</p>")

    result = await validate_compile_time(page, ByTestidTarget(testid="wyniki"), "highlight")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_found"
