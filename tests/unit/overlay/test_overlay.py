from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.overlay.overlay import Overlay


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def test_install_injects_cursor_into_current_document(page: Page) -> None:
    overlay = Overlay()

    await overlay.install(page)

    assert await page.evaluate("!!window.__guidebot_cursor") is True
    contract = await page.evaluate(
        """() => ({
            methods: ["ensure", "moveTo", "ripple", "highlight"].map(
                (name) => typeof window.__guidebot_cursor[name]
            ),
            position: getComputedStyle(
                document.querySelector("[data-guidebot-cursor]")
            ).position,
            pointerEvents: getComputedStyle(
                document.querySelector("[data-guidebot-cursor]")
            ).pointerEvents,
            zIndex: getComputedStyle(
                document.querySelector("[data-guidebot-cursor]")
            ).zIndex,
        })"""
    )
    assert contract == {
        "methods": ["function"] * 4,
        "position": "fixed",
        "pointerEvents": "none",
        "zIndex": "2147483647",
    }


async def test_install_registers_cursor_for_future_documents(page: Page) -> None:
    overlay = Overlay()
    await overlay.install(page)

    await page.goto("data:text/html,<main>next document</main>")

    assert await page.evaluate("!!window.__guidebot_cursor") is True
    assert await page.locator("[data-guidebot-cursor]").count() == 1


async def test_move_to_updates_dom_and_python_position(page: Page) -> None:
    overlay = Overlay()
    await overlay.install(page)

    await overlay.move_to(page, 100, 100)

    left = await page.locator("[data-guidebot-cursor]").evaluate(
        "element => element.getBoundingClientRect().left"
    )
    assert left == pytest.approx(100, abs=1)
    assert overlay.pos == (100.0, 100.0)


async def test_ensure_recreates_wiped_dom_and_restores_position(page: Page) -> None:
    overlay = Overlay()
    await overlay.install(page)
    await overlay.move_to(page, 75, 125, ms=0)

    await page.set_content("<main>SPA rerender</main>")
    assert await page.locator("[data-guidebot-cursor]").count() == 0

    await overlay.ensure(page)

    assert await page.evaluate("!!window.__guidebot_cursor") is True
    cursor = page.locator("[data-guidebot-cursor]")
    assert await cursor.count() == 1
    box = await cursor.bounding_box()
    assert box is not None
    assert box["x"] == pytest.approx(75, abs=1)
    assert box["y"] == pytest.approx(125, abs=1)
