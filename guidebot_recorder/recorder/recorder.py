"""Recorder — the Python API that drives the browser (§6).

The only place that "knows how": it builds a locator from the frozen `Target`
fields, animates the cursor (overlay), and performs the action via Playwright.
The overlay is optional — the `compile` phase needs no animation, so it can use
`Recorder(page, None)`.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Locator, Page

from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.resolver.validate import build_locator

# WaitState → the state accepted by Playwright's locator.wait_for
_WAIT_STATE: dict[str, str] = {"visible": "visible", "hidden": "hidden", "enabled": "visible"}


class Recorder:
    def __init__(self, page: Page, overlay: Overlay | None, settle_ms: float = 280) -> None:
        self.page = page
        self.overlay = overlay
        # Pause (ms) after the cursor lands and ripples, before the action fires —
        # gives the viewer a beat to register *where* the cursor stopped.
        self.settle_ms = settle_ms

    async def _point_and_prepare(self, target: Target) -> Locator:
        locator = await build_locator(self.page, target)
        # scroll to the target on BOTH axes — an element can be off-screen horizontally
        # too, and Playwright's auto-scroll is vertically centric
        await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        if self.overlay is not None:
            box = await locator.bounding_box()
            if box is not None:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await self.overlay.move_to(self.page, cx, cy)
                await self.overlay.ripple(self.page)
                await self.page.wait_for_timeout(self.settle_ms)
        return locator

    async def navigate(self, url: str) -> None:
        await self.page.goto(url)
        await self.apply_readiness("navigation")

    async def click(self, target: Target) -> None:
        locator = await self._point_and_prepare(target)
        await locator.click()

    async def hover(self, target: Target) -> None:
        locator = await self._point_and_prepare(target)
        await locator.hover()

    async def enter_text(self, target: Target, text: str) -> None:
        locator = await self._point_and_prepare(target)
        await locator.fill(text)

    async def wait_seconds(self, seconds: float) -> None:
        # A wall-clock pause must survive a popup closing while the pause is in
        # progress; binding it to Page.wait_for_timeout would raise TargetClosedError.
        await asyncio.sleep(seconds)

    async def wait_for(self, target: Target, state: WaitState, timeout: float) -> None:
        locator = await build_locator(self.page, target)
        await locator.wait_for(state=_WAIT_STATE[state], timeout=timeout * 1000)

    async def apply_readiness(self, expect: Expect) -> None:
        if expect == "navigation":
            await self.page.wait_for_load_state()
        elif expect == "idle":
            await self.page.wait_for_load_state("networkidle")
        else:
            await self.page.wait_for_timeout(100)
