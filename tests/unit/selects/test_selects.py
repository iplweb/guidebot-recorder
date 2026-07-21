"""Tests for the Python ``Selects`` controller (config prelude + installation)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import BrowserContext, async_playwright

from guidebot_recorder.models.config import SelectsConfig
from guidebot_recorder.selects import Selects
from guidebot_recorder.selects.selects import SelectsNotReadyError


def _prelude(script: str) -> dict:
    match = re.match(r"window\.__guidebot_selects_config = (\{.*\});\n", script)
    assert match is not None, script[:200]
    return json.loads(match.group(1))


def test_prelude_carries_the_config_as_camel_case_json() -> None:
    selects = Selects(SelectsConfig(mode="native", settle_ms=250, max_visible_options=3))
    assert _prelude(selects.script) == {
        "mode": "native",
        "settleMs": 250,
        "maxVisibleOptions": 3,
    }


def test_defaults_are_used_when_no_config_is_given() -> None:
    assert _prelude(Selects().script) == _prelude(Selects(SelectsConfig()).script)
    assert _prelude(Selects().script)["mode"] == "shim"


def test_open_hold_ms_stays_python_side() -> None:
    """It paces the recorder's second beat; the widget has no use for it."""
    assert "openHoldMs" not in _prelude(Selects(SelectsConfig(open_hold_ms=900)).script)


def test_script_is_the_prelude_followed_by_the_widget_body() -> None:
    script = Selects().script
    assert script.startswith("window.__guidebot_selects_config = ")
    assert "__guidebot_selects" in script
    assert "data-guidebot-select-button" in script


@pytest.fixture
async def context() -> AsyncIterator[BrowserContext]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        try:
            yield ctx
        finally:
            await browser.close()


async def test_install_context_shims_every_new_document(context: BrowserContext) -> None:
    selects = Selects(SelectsConfig(settle_ms=20))
    await selects.install_context(context)
    page = await context.new_page()
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:200px'>"
        "<option>a</option><option>b</option></select></body>"
    )
    await selects.wait_ready(page)
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )


async def test_install_context_reaches_nested_iframes(context: BrowserContext) -> None:
    """A select in a nested iframe needs shimming just as much (spec §1)."""
    selects = Selects(SelectsConfig(settle_ms=20))
    await selects.install_context(context)
    page = await context.new_page()
    await page.set_content("<body style='margin:0'><iframe id='f' srcdoc=\"\"></iframe></body>")
    await page.evaluate(
        """() => new Promise((resolve) => {
      const frame = document.getElementById('f');
      frame.addEventListener('load', resolve, {once: true});
      frame.srcdoc = "<body style='margin:0'><select id='s' style='width:200px'>"
        + "<option>a</option><option>b</option></select></body>";
    })"""
    )
    child = page.frames[1]
    await selects.wait_ready(child)
    assert await child.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )


async def test_wait_ready_fails_loudly_when_the_widget_is_absent(
    context: BrowserContext,
) -> None:
    page = await context.new_page()
    await page.set_content("<div></div>")
    with pytest.raises(Exception, match="guidebot selects API is unavailable"):
        await Selects().wait_ready(page)


async def test_wait_ready_times_out_instead_of_hanging_forever(context: BrowserContext) -> None:
    """C1: a `ready` that never settles must fail loudly, not hang the render."""
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate(
        "() => { window.__guidebot_selects = {ready: new Promise(() => {})}; }",
    )
    with pytest.raises(SelectsNotReadyError, match="nie zgłosił gotowości"):
        await Selects().wait_ready(page, timeout=0.4)


async def test_wait_ready_timeout_leaves_a_working_page(context: BrowserContext) -> None:
    """The timeout must not wedge the connection: the frame stays usable."""
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate("() => { window.__guidebot_selects = {ready: new Promise(() => {})}; }")
    with pytest.raises(SelectsNotReadyError):
        await Selects().wait_ready(page, timeout=0.4)
    assert await page.evaluate("() => 1 + 1") == 2
