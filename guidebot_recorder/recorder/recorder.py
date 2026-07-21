"""Recorder — the Python API that drives the browser (§6).

The only place that "knows how": it builds a locator from the frozen `Target`
fields, animates the cursor (overlay), and performs the action via Playwright.
The overlay is optional — the `compile` phase needs no animation, so it can use
`Recorder(page, None)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import NamedTuple

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from guidebot_recorder.chrome.typing import DEFAULT_MAX_DELAY_FACTOR, typing_schedule
from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.scenario import Scroll
from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.resolver.validate import build_locator

# WaitState → the state accepted by Playwright's locator.wait_for
_WAIT_STATE: dict[str, str] = {"visible": "visible", "hidden": "hidden", "enabled": "visible"}


class PointResult(NamedTuple):
    locator: Locator
    box: dict | None
    center: tuple[float, float] | None


class Recorder:
    def __init__(
        self,
        page: Page,
        overlay: Overlay | None,
        settle_ms: float = 280,
        frame: Page | Frame | None = None,
        *,
        type_delay_ms: float | None = None,
        type_jitter_ms: int = 0,
        type_max_delay_factor: float = DEFAULT_MAX_DELAY_FACTOR,
        on_sfx: Callable[[str], None] | None = None,
    ) -> None:
        self.page = page
        # Locators, navigation and reuse-validation run against ``frame``; the
        # main window drives the site iframe (a ``Frame``) while the overlay and
        # load-state stay on the page. ``frame`` defaults to the page, which is
        # the popup/compile/chrome-disabled case (frame is the page).
        self.frame: Page | Frame = frame if frame is not None else page
        self.overlay = overlay
        # Pause (ms) after the cursor lands and ripples, before the action fires —
        # gives the viewer a beat to register *where* the cursor stopped.
        self.settle_ms = settle_ms
        # Per-character pause (ms) for the animated `enter_text` path; None keeps
        # the instant `locator.fill()` behavior (compile-mode / no polish).
        self._type_delay_ms = type_delay_ms
        # ± jitter (ms) around the per-character delay so form typing reads as
        # natural as the address bar, not metronomic.
        self._type_jitter_ms = type_jitter_ms
        # Hard ceiling on one character's delay, as a multiple of the base delay.
        self._type_max_delay_factor = type_max_delay_factor
        # Sound-effect hook, called with "click" or "key"; None means silent.
        self._on_sfx = on_sfx

    @property
    def on_sfx(self) -> Callable[[str], None] | None:
        """The SFX hook (or ``None`` when muted) — reused for the address bar."""
        return self._on_sfx

    async def point(
        self, target: Target, *, ripple: bool = True, click_sound: bool = False
    ) -> PointResult:
        """Resolve the target, scroll it into view, move the cursor onto it.

        Returns the locator plus the target's bounding box and center (viewport
        pixels) so callers (e.g. the PDF guide) can annotate without re-resolving.
        ``ripple=False`` suppresses the click ring — a still capture wants a
        clean frame. ``box``/``center`` are None when the element has no box.
        """
        locator = await build_locator(self.frame, target)
        # scroll to the target on BOTH axes — an element can be off-screen horizontally
        # too, and Playwright's auto-scroll is vertically centric
        await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        box: dict | None = None
        center: tuple[float, float] | None = None
        rippled = False
        if self.overlay is not None:
            box = await locator.bounding_box()
            if box is not None:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                center = (cx, cy)
                await self.overlay.move_to(self.page, cx, cy)
                if ripple:
                    await self.overlay.ripple(self.page, flash=click_sound)
                    if click_sound and self._on_sfx is not None:
                        self._on_sfx("click")  # AT ripple time, before the settle pause
                    rippled = True
                    await self.page.wait_for_timeout(self.settle_ms)
        if click_sound and not rippled and self._on_sfx is not None:
            self._on_sfx("click")  # fallback: no overlay / no bbox
        return PointResult(locator, box, center)

    async def _point_and_prepare(self, target: Target, *, click_sound: bool = False) -> Locator:
        res = await self.point(target, ripple=True, click_sound=click_sound)
        return res.locator

    async def navigate(self, url: str) -> None:
        # For the main window ``self.frame`` is the site iframe, so this navigates
        # the iframe (not the shell); for popups/compile it is the page itself.
        await self.frame.goto(url)
        await self.apply_readiness("navigation")

    async def click(
        self,
        target: Target,
        *,
        before_click: Callable[[], None] | None = None,
    ) -> None:
        locator = await self._point_and_prepare(target, click_sound=True)
        if before_click is not None:
            before_click()
        await locator.click()

    async def hover(self, target: Target) -> None:
        locator = await self._point_and_prepare(target)
        await locator.hover()

    async def enter_text(self, target: Target, text: str) -> None:
        locator = await self._point_and_prepare(target)  # no click sound, no flash
        if self._type_delay_ms is None or any(c in text for c in "\n\r\t"):
            await locator.fill(text)
            return
        await locator.fill("")
        # Same natural feel as the address bar: a jittered per-character schedule
        # (no segment pauses — those are for URLs). ``delays[j]`` is the pre-delay
        # for char j; the first char types immediately (delays[0] unused), then each
        # subsequent char waits its own jittered pause. Seeded by the text so a
        # re-render types identically.
        delays = typing_schedule(
            text,
            char_delay_ms=int(self._type_delay_ms),
            char_jitter_ms=self._type_jitter_ms,
            segment_pause_ms=0,
            seed=text,
            max_delay_factor=self._type_max_delay_factor,
        )
        for i, ch in enumerate(text):
            await locator.press_sequentially(ch)
            if self._on_sfx is not None:
                self._on_sfx("key")
            if i < len(text) - 1:
                await self.page.wait_for_timeout(delays[i + 1])
        try:
            needs_fix = await locator.input_value() != text
        except PlaywrightError:
            needs_fix = True  # non-input target (e.g. contenteditable): re-issue fill()
        if needs_fix:
            await locator.fill(text)

    async def select(self, target: Target, option: str) -> None:
        """Choose ``option`` (a visible label) from a native ``<select>``.

        A native select's option list is drawn by the OS, so no browser-automation
        tool can unfurl or screenshot it. With an overlay (render) the cursor
        glides to the control, ripples, and the value is *stepped* to ``option``
        with arrow keys so the change is visible on the collapsed control; without
        one (compile) the value is set directly. Either way the element ends on
        ``option``, so later steps and the render agree.
        """

        locator = await self._point_and_prepare(target, click_sound=True)
        if self.overlay is None:
            await locator.select_option(label=option)
            return
        await self._step_option_visibly(locator, option)

    async def _step_option_visibly(self, locator: Locator, option: str) -> None:
        await locator.focus()
        plan = await locator.evaluate(
            """(el, wanted) => {
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const labels = Array.from(el.options, (o) => norm(o.label || o.textContent));
                const want = norm(wanted);
                let target = labels.indexOf(want);
                if (target < 0) {
                    const lower = want.toLowerCase();
                    target = labels.findIndex((l) => l.toLowerCase() === lower);
                }
                return { target, current: el.selectedIndex };
            }""",
            option,
        )
        target_index = plan["target"]
        if target_index < 0:
            # Unknown label — let Playwright raise its clear "no option" error.
            await locator.select_option(label=option)
            return
        steps = target_index - plan["current"]
        # A far jump would drag on arrow-by-arrow; set it directly instead.
        if abs(steps) > 12:
            await locator.select_option(index=target_index)
            return
        key = "ArrowDown" if steps > 0 else "ArrowUp"
        for _ in range(abs(steps)):
            await locator.press(key)
            if self._on_sfx is not None:
                self._on_sfx("key")
            await self.page.wait_for_timeout(140)
        # Guarantee the final value even if a browser skipped a disabled option.
        if await locator.evaluate("el => el.selectedIndex") != target_index:
            await locator.select_option(index=target_index)

    async def scroll(self, spec: Scroll) -> None:
        """Scroll the site — a render-only visual with no agent target.

        Content the resolver cannot target (native-select option lists, iframe
        previews) still appears in the recording; scrolling brings below-the-fold
        content into view. With an overlay (render) the scroll is animated as a
        stepped glide; without one (compile) it jumps directly.
        """

        metrics = await self.frame.evaluate(
            "() => ({ y: window.scrollY, vh: window.innerHeight,"
            " max: Math.max(0, document.documentElement.scrollHeight - window.innerHeight) })"
        )
        cur, vh, maxy = float(metrics["y"]), float(metrics["vh"]), float(metrics["max"])
        if spec.to == "top":
            target = 0.0
        elif spec.to == "bottom":
            target = maxy
        else:
            step_px = spec.amount if spec.amount is not None else vh * 0.85
            target = cur + step_px if spec.to == "down" else cur - step_px
        target = max(0.0, min(target, maxy))
        if self.overlay is None or abs(target - cur) < 1.0:
            await self.frame.evaluate("(y) => window.scrollTo(0, y)", target)
            return
        steps = 16
        for i in range(1, steps + 1):
            y = cur + (target - cur) * (i / steps)
            await self.frame.evaluate("(y) => window.scrollTo(0, y)", y)
            await self.page.wait_for_timeout(18)
        await self.page.wait_for_timeout(150)

    async def wait_seconds(self, seconds: float) -> None:
        # A wall-clock pause must survive a popup closing while the pause is in
        # progress; binding it to Page.wait_for_timeout would raise TargetClosedError.
        await asyncio.sleep(seconds)

    async def wait_for(self, target: Target, state: WaitState, timeout: float) -> None:
        locator = await build_locator(self.frame, target)
        await locator.wait_for(state=_WAIT_STATE[state], timeout=timeout * 1000)

    async def apply_readiness(self, expect: Expect) -> None:
        if expect == "navigation":
            await self.page.wait_for_load_state()
        elif expect == "idle":
            await self.page.wait_for_load_state("networkidle")
        else:
            await self.page.wait_for_timeout(100)
