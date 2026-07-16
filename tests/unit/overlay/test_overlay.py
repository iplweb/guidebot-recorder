from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.models.config import CursorClick, CursorConfig, Viewport
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


async def test_context_install_injects_cursor_into_popup(page: Page) -> None:
    overlay = Overlay()
    await overlay.install_context(page.context)
    await page.set_content("<button onclick=\"window.open('about:blank')\">Open</button>")

    async with page.expect_popup() as popup_info:
        await page.get_by_role("button", name="Open").click()
    popup = await popup_info.value

    assert await popup.evaluate("!!window.__guidebot_cursor") is True
    await popup.goto("data:text/html,<main>replacement document</main>")
    await popup.wait_for_load_state()
    assert await popup.evaluate("!!window.__guidebot_cursor") is True
    assert await popup.locator("[data-guidebot-cursor]").count() == 1


async def test_cursor_config_drives_size_and_glide(page: Page) -> None:
    overlay = Overlay(
        CursorConfig(width=50, height=68, speed=2.0, min_duration=0, max_duration=9999)
    )
    await overlay.install(page)

    size = await page.locator("[data-guidebot-cursor]").evaluate(
        "el => [el.getBoundingClientRect().width, el.getBoundingClientRect().height]"
    )
    assert size == [50, 68]

    # distance-proportional: 200px at 2.0 px/ms → ~100ms (defaults would clamp to 320)
    assert overlay._glide_duration((0.0, 0.0), (200.0, 0.0)) == pytest.approx(100.0)


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


def test_overlay_viewport_centers_pos_backcompat_zero() -> None:
    assert Overlay().pos == (0.0, 0.0)
    assert Overlay(CursorConfig()).pos == (0.0, 0.0)
    o = Overlay(CursorConfig(), Viewport(width=1000, height=400))
    assert o.pos == (500.0, 200.0)


def test_prelude_carries_click_and_start() -> None:
    import json
    import re

    o = Overlay(CursorConfig(click=CursorClick(flash=True)), Viewport(width=800, height=600))
    prelude = o._script.split("\n", 1)[0]
    cfg = json.loads(re.search(r"= (\{.*\});", prelude).group(1))
    assert cfg["click"]["flash"] is True
    assert cfg["start"] == [400.0, 300.0]


async def test_hide_show_and_ripple_flash(page: Page) -> None:
    overlay = Overlay(CursorConfig(click=CursorClick(flash=True)), Viewport(width=800, height=600))
    await overlay.install(page)
    await overlay.hide(page)
    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp == "none"
    await overlay.show(page)
    await overlay.ripple(page, flash=True)  # must not raise; TypeError would fail the test


async def test_default_overlay_ripple_is_backcompat(page: Page) -> None:
    """Zero-config default ripple must stay byte-identical to today's ring.

    The default CursorConfig.click (color rgba(37,99,235,.9), scale 3.25,
    flash False) and ripple() with no flash kwarg must produce exactly one ring
    in the default blue, its animation ending at scale(3.25), and NO flash disc.
    """
    overlay = Overlay()  # no viewport, default CursorConfig → default CFG.click
    await overlay.install(page)
    await overlay.ripple(page)  # no flash kwarg → flash defaults False

    # The ring lives ~500ms; read it synchronously in one evaluate.
    result = await page.evaluate(
        """() => {
            const rings = document.querySelectorAll('[data-guidebot-ripple]');
            const flashes = document.querySelectorAll('[data-guidebot-flash]');
            const ring = rings[0];
            const anim = ring.getAnimations()[0];
            const frames = anim.effect.getKeyframes();
            return {
                ringCount: rings.length,
                flashCount: flashes.length,
                borderTopColor: getComputedStyle(ring).borderTopColor,
                endTransform: frames[frames.length - 1].transform,
            };
        }"""
    )
    assert result["ringCount"] == 1
    assert result["flashCount"] == 0
    assert result["borderTopColor"] == "rgba(37, 99, 235, 0.9)"
    assert result["endTransform"] == "scale(3.25)"
