"""Recorder — the Python API that drives the browser (§6).

The only place that "knows how": it builds a locator from the frozen `Target`
fields, animates the cursor (overlay), and performs the action via Playwright.
The overlay is optional — the `compile` phase needs no animation, so it can use
`Recorder(page, None)`.

One command does not fit in here: `select:`. Driving a dropdown on camera is a
choreography with its own vocabulary — beats, reveal hooks, three page shapes,
nine distinct refusals — and it used to be 72% of this class while no other
method called into it. It now lives in `recorder/select/`, and this module keeps
the two delegators (`select`, `diagnose_select`) plus a re-export of the error
type, so that every existing caller keeps its import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import NamedTuple

from playwright.async_api import ElementHandle, Frame, Locator, Page
from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.chrome.typing import DEFAULT_MAX_DELAY_FACTOR, typing_schedule
from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.scenario import ResolvedHighlight, Scroll
from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.geometry import center_of, ellipse_around, fit_to_bounds
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.select import (
    OPTION_MISSING,
    UNDRIVABLE,
    RevealHook,
    SelectDriveError,
    SelectDriver,
    SelectReveal,
)
from guidebot_recorder.resolver.validate import build_locator

#: Re-exported for the callers that have always imported them from here: the
#: compile and render step loops, the PDF guide's capture loop, and three test
#: modules. `reason` is a cross-layer contract — `OPTION_MISSING` is the one
#: refusal an `optional:` step may skip, everything else must stop the guide —
#: so moving the type would have rewritten six unrelated files for no gain.
#:
#: `OPTION_WAIT_MS` and `READY_WAIT_MS` are pointedly *not* here; see the
#: `recorder/select/__init__.py` docstring for why re-exporting a patched name
#: is worse than an import error.
__all__ = [
    "OPTION_MISSING",
    "UNDRIVABLE",
    "PointResult",
    "Recorder",
    "RevealHook",
    "SelectDriveError",
    "SelectReveal",
]

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
        open_hold_ms: float = 350,
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
        # Pause (ms) after the option list unfurls, before the cursor sets off
        # towards the wanted row — the viewer needs a beat to read the list.
        # Mirrors ``SelectsConfig.open_hold_ms``; the caller passes the config
        # value, this default only has to be sane on its own.
        self.open_hold_ms = open_hold_ms

    @property
    def on_sfx(self) -> Callable[[str], None] | None:
        """The SFX hook (or ``None`` when muted) — reused for the address bar."""
        return self._on_sfx

    async def _approach(
        self,
        control: Locator | ElementHandle,
        *,
        ripple: bool = True,
        click_sound: bool = False,
    ) -> tuple[dict | None, tuple[float, float] | None]:
        """Glide the cursor onto ``control``, ripple, and settle.

        Takes a ``Locator`` *or* an ``ElementHandle`` because the select
        choreography aims at nodes that no frozen ``Target`` names — a page
        widget resolved by the association heuristic, or an option row that only
        exists once the list is open.

        Returns the control's bounding box and center (viewport pixels), both
        ``None`` when it has no box, so :meth:`point` can hand them to callers
        that annotate the frame without re-resolving.
        """

        # scroll to the target on BOTH axes — an element can be off-screen horizontally
        # too, and Playwright's auto-scroll is vertically centric
        await control.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        box: dict | None = None
        center: tuple[float, float] | None = None
        rippled = False
        if self.overlay is not None:
            box = await control.bounding_box()
            center = center_of(box)
            if center is not None:
                cx, cy = center
                await self.overlay.move_to(self.page, cx, cy)
                if ripple:
                    await self.overlay.ripple(self.page, flash=click_sound)
                    if click_sound and self._on_sfx is not None:
                        self._on_sfx("click")  # AT ripple time, before the settle pause
                    rippled = True
                    await self.page.wait_for_timeout(self.settle_ms)
        if click_sound and not rippled and self._on_sfx is not None:
            self._on_sfx("click")  # fallback: no overlay / no bbox
        return box, center

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
        box, center = await self._approach(locator, ripple=ripple, click_sound=click_sound)
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
        try:
            await locator.click()
        except PlaywrightError:
            # A click whose handler closes the window (a popup's own "Zamknij",
            # a logout that shuts the tab) races Playwright's post-dispatch
            # bookkeeping: the click *lands* — the call log ends at "performing
            # click action" — and only then the target disappears. Whether the
            # error surfaces at all depends on which side wins, so re-raising it
            # would make a supported ending fail at random.
            #
            # Liveness is read back from the window rather than sniffed out of
            # the message: the caller's lifecycle checkpoints act on that same
            # state, and they say far more precisely than Playwright can whether
            # this close was the scenario's doing. A window still alive means the
            # click genuinely failed, so that error keeps travelling.
            if not self.page.is_closed():
                raise

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

    def _select_driver(self) -> SelectDriver:
        """The choreography, handed five narrow things instead of this object.

        Built per call rather than kept on the instance. It is cheap — five
        attribute reads — and it cannot go stale: whatever the recorder is
        pointing at *now* is what the driver gets, with no second copy of
        ``frame`` to keep in step with the original.

        ``approach`` is a lambda, and that is load-bearing rather than
        stylistic. ``tests/unit/recorder/test_recorder_select.py`` replaces
        ``_approach`` **on the instance** to sample the option list's geometry
        either side of every cursor glide, and the lambda resolves the attribute
        at call time, so the spy is what runs. Passing ``self._approach`` instead
        happens to work *today* only because this method runs after the test has
        already installed its spy; hoist the driver into ``__init__`` — an
        obvious-looking optimisation — and a bound method here would freeze the
        original, the spy would observe nothing, and the test asserting that the
        list is scrolled before the cursor sets off would pass while checking
        nothing. Two locks on the same door, and this is the one that survives
        the hoist.
        """

        return SelectDriver(
            page=self.page,
            frame=self.frame,
            approach=lambda *args, **kwargs: self._approach(*args, **kwargs),
            # The driver never touches the overlay — it only ever asks whether
            # one exists, so it is handed that question's answer, not the object.
            animated=self.overlay is not None,
            open_hold_ms=self.open_hold_ms,
        )

    async def select(
        self,
        target: Target,
        option: str,
        *,
        native: bool = False,
        ripple: bool = True,
        on_revealed: RevealHook | None = None,
    ) -> None:
        """Choose ``option`` (a visible label) from a ``<select>``, on camera.

        The whole contract — the three page shapes, the ``native`` escape hatch,
        the exact instant ``on_revealed`` fires, and every way this can refuse —
        is documented on :meth:`~...select.driver.SelectDriver.select`, which
        does the work.
        """

        await self._select_driver().select(
            target, option, native=native, ripple=ripple, on_revealed=on_revealed
        )

    async def diagnose_select(self, target: Target, option: str) -> SelectDriveError:
        """Why this ``<select>`` cannot be revealed, phrased for the author.

        Returned rather than raised, so the PDF guide can wrap it in its own step
        banner. See :meth:`~...select.driver.SelectDriver.diagnose`.
        """

        return await self._select_driver().diagnose(target, option)

    async def highlight(self, target: Target, spec: ResolvedHighlight) -> None:
        """Lap an ellipse around the target, leaving a marker trail — touching nothing.

        The one command that points at the page without changing it: no click, no
        hover, no DOM event. Only ``render`` calls this — ``compile`` freezes the
        target without acting, and the PDF guide draws its own still ellipse — so
        an overlay is always present in practice.

        The guard below is defensive rather than a real degradation path: every
        route here passes validation that rejects ``not_visible``, and a visible
        element has a bounding box. If that ever stops holding, drawing nothing
        beats failing a render over a decorative mark.
        """

        result = await self.point(target, ripple=False)
        if self.overlay is None or result.box is None:
            return
        viewport = self.page.viewport_size or {"width": 1280, "height": 720}
        ellipse = fit_to_bounds(
            ellipse_around(result.box, spec.padding),
            width=float(viewport["width"]),
            height=float(viewport["height"]),
        )
        # Glide to the lap's entry point first: `point` left the cursor in the
        # middle of the target, and starting the lap from there would teleport it
        # by `rx` — several hundred pixels for anything table-sized.
        await self.overlay.move_to(self.page, ellipse.cx + ellipse.rx, ellipse.cy)
        await self.overlay.encircle(
            self.page,
            cx=ellipse.cx,
            cy=ellipse.cy,
            rx=ellipse.rx,
            ry=ellipse.ry,
            loops=spec.loops,
            hold=spec.hold,
            color=spec.color,
        )

    async def scroll(self, spec: Scroll) -> None:
        """Scroll the site — a render-only visual with no agent target.

        Content the resolver cannot target (an iframe preview, for instance)
        still appears in the recording; scrolling brings below-the-fold content
        into view. With an overlay (render) the scroll is animated as a stepped
        glide; without one (compile) it jumps directly.

        A native ``<select>``'s option list used to head that list and no longer
        does: ``selects.js`` renders it into the DOM, so :meth:`select` drives it
        directly and nothing about it needs scrolling into frame here.
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
