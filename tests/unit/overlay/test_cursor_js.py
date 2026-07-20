"""Direct tests of cursor.js's public API (no Python Overlay wrapper).

Injects ``window.__guidebot_cursor_config`` then evaluates the raw script,
exercising the ``CFG.start`` seed, the configurable/optional-flash ripple, and
the persistent ``hidden`` flag added in Task 1.1.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import Page, async_playwright


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page()
        try:
            yield pg
        finally:
            await b.close()


CURSOR_JS = files("guidebot_recorder.overlay").joinpath("cursor.js").read_text("utf-8")


async def _inject(page: Page, cfg: dict) -> None:
    await page.evaluate(f"window.__guidebot_cursor_config = {json.dumps(cfg)};")
    await page.evaluate(CURSOR_JS)


async def test_start_seed_centers_first_mount(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"start": [400, 300]})
    pos = await page.evaluate("window.__guidebot_cursor.position")
    assert pos == [400, 300]


async def test_ripple_flash_draws_filled_disc_only_when_configured_and_requested(
    page: Page,
) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"click": {"color": "rgb(1,2,3)", "scale": 5, "flash": True}})
    # ripple(true) synchronously creates the ring (+ flash disc); read immediately.
    n = await page.evaluate(
        "() => { window.__guidebot_cursor.ripple(true);"
        " return document.querySelectorAll('[data-guidebot-ripple],[data-guidebot-flash]').length; }"
    )
    assert n >= 2  # ring + flash
    # flash=false → ring only
    n2 = await page.evaluate(
        "() => { document.querySelectorAll('[data-guidebot-flash]').forEach(e=>e.remove());"
        " window.__guidebot_cursor.ripple(false);"
        " return document.querySelectorAll('[data-guidebot-flash]').length; }"
    )
    assert n2 == 0


# Chrome serializes computed `contain` using the shorthand keywords, so
# `layout style paint` comes back as `content`. Expand before asserting.
_CONTAIN_SHORTHANDS = {
    "content": {"layout", "paint", "style"},
    "strict": {"layout", "paint", "size", "style"},
}


def _contain_keywords(computed: str) -> set[str]:
    out: set[str] = set()
    for token in computed.split():
        out |= _CONTAIN_SHORTHANDS.get(token, {token})
    return out


async def test_cursor_host_does_not_paint_contain(page: Page) -> None:
    """`contain: paint` clips the drop-shadow glow to the 34x46 host box."""
    await page.set_content("<div></div>")
    await _inject(page, {})
    contain = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).contain"
    )
    keywords = _contain_keywords(contain)
    assert "paint" not in keywords, f"glow is clipped by contain: {contain!r}"
    # layout/style isolation is what the declaration is there for; keep it.
    assert {"layout", "style"} <= keywords, f"lost isolation: {contain!r}"


async def test_hidden_flag_survives_ensure(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {})
    await page.evaluate("window.__guidebot_cursor.hide()")
    await page.evaluate("window.__guidebot_cursor.ensure()")
    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp == "none"
    await page.evaluate("window.__guidebot_cursor.show()")
    disp2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp2 == "block"
