"""Recorder — the Python API that drives the browser (§6).

The only place that "knows how": it builds a locator from the frozen `Target`
fields, animates the cursor (overlay), and performs the action via Playwright.
The overlay is optional — the `compile` phase needs no animation, so it can use
`Recorder(page, None)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from playwright.async_api import ElementHandle, Frame, Locator, Page
from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.chrome.typing import DEFAULT_MAX_DELAY_FACTOR, typing_schedule
from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.scenario import Scroll
from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.resolver.validate import build_locator
from guidebot_recorder.resolver.widget import associated_control

# WaitState → the state accepted by Playwright's locator.wait_for
_WAIT_STATE: dict[str, str] = {"visible": "visible", "hidden": "hidden", "enabled": "visible"}

#: How long the second beat waits for the option row to exist and be visible.
#: Generous, because a page widget may build its list asynchronously (or fetch
#: it), but bounded: a list that never appears must fail, not hang.
OPTION_WAIT_MS = 5000


class SelectDriveError(RuntimeError):
    """A ``select:`` step could not be performed *visibly*.

    Raised when the choreography has nothing for the cursor to click: the page
    enhanced the ``<select>`` itself and the association heuristic found no
    visible control, or the unfurled list never produced a row matching the
    option label within :data:`OPTION_WAIT_MS`.

    There is deliberately no fallback to ``select_option()``. Falling back would
    restore exactly the invisible value change this choreography exists to
    remove, and would do it unobservably — the run would succeed and only a
    viewer would ever discover the step is unwatchable.

    The render layer catches this and re-raises it as a ``RenderError`` carrying
    the index of the failing step. ``RenderError`` lives in ``render.py``, which
    imports this module, so it cannot be raised from here without a cycle.
    """


#: (el) => {installed, shimmed} — how this select relates to the shim.
#:
#: ``installed`` distinguishes "the widget ran and decided not to shim this
#: select" (so the page enhanced it itself, or ``mode: native`` is in force)
#: from "no shim layer here at all" — a bare context such as a health probe or
#: a unit-test page. Only the former says anything about drivability.
_SHIM_STATE_JS = """(el) => {
  const api = window.__guidebot_selects;
  return { installed: !!api, shimmed: !!(api && api.isShimmed(el)) };
}"""

#: The readiness barrier of spec §3, read straight off the page.
#:
#: Deliberately not routed through :class:`guidebot_recorder.selects.Selects`:
#: the recorder is handed a page, not the controller that installed the widget,
#: and a missing API must degrade to "nothing to wait for", not to an error.
_SELECTS_READY_JS = "() => window.__guidebot_selects && window.__guidebot_selects.ready"

#: A short, human-readable name for a control, for error messages.
_DESCRIBE_JS = """(el) => {
  const parts = [el.tagName.toLowerCase()];
  if (el.id) parts.push("#" + el.id);
  const name = el.getAttribute("name");
  if (name) parts.push(`[name="${name}"]`);
  const label = el.getAttribute("aria-label");
  if (label) parts.push(`[aria-label="${label}"]`);
  return parts.join("");
}"""

#: Remember every element that existed *before* the list was opened, so the
#: second beat can tell the page's freshly-rendered option rows from whatever
#: already carried the same text elsewhere on the page.
_SNAPSHOT_JS = """() => {
  window.__guidebot_select_snapshot = new Set(document.querySelectorAll("*"));
}"""

#: (label) => Element | null — the first *newly added* visible element whose
#: trimmed text is exactly the option label. ``querySelectorAll`` yields
#: document order, so the first hit is the document-order tie-break of spec §4.
_APPEARED_NODE_JS = """(label) => {
  const seen = window.__guidebot_select_snapshot;
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const wanted = norm(label);
  for (const node of document.querySelectorAll("*")) {
    if (seen && seen.has(node)) continue;
    if (norm(node.textContent) !== wanted) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    return node;
  }
  return null;
}"""


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
        click_sound: bool = False,
    ) -> None:
        """Glide the cursor onto ``control``, ripple, and settle.

        Takes a ``Locator`` *or* an ``ElementHandle`` because the select
        choreography aims at nodes that no frozen ``Target`` names — a page
        widget resolved by the association heuristic, or an option row that only
        exists once the list is open.
        """

        # scroll to the target on BOTH axes — an element can be off-screen horizontally
        # too, and Playwright's auto-scroll is vertically centric
        await control.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        rippled = False
        if self.overlay is not None:
            box = await control.bounding_box()
            if box is not None:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await self.overlay.move_to(self.page, cx, cy)
                await self.overlay.ripple(self.page, flash=click_sound)
                if click_sound and self._on_sfx is not None:
                    self._on_sfx("click")  # AT ripple time, before the settle pause
                rippled = True
                await self.page.wait_for_timeout(self.settle_ms)
        if click_sound and not rippled and self._on_sfx is not None:
            self._on_sfx("click")  # fallback: no overlay / no bbox

    async def _point_and_prepare(self, target: Target, *, click_sound: bool = False) -> Locator:
        locator = await build_locator(self.frame, target)
        await self._approach(locator, click_sound=click_sound)
        return locator

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

    async def select(self, target: Target, option: str, *, native: bool = False) -> None:
        """Choose ``option`` (a visible label) from a ``<select>``, on camera.

        A native select draws its option list as an OS popup, which the
        screencast cannot record; ``selects.js`` therefore replaces that list
        with a DOM one, and this method drives it in two visible beats (spec
        §4): the cursor glides to the control and clicks it, the list unfurls,
        then the cursor glides to the wanted row and clicks that. Pages that
        enhance their own selects (select2, Tom Select) already have a DOM list;
        those are driven the same way, only the two click targets differ.

        Args:
            target: the frozen target of the ``<select>`` itself — never of the
                widget standing in for it, which no ``Target`` names.
            option: the visible label to choose.
            native: the ``mode: native`` escape hatch, already resolved by the
                caller from the per-step override and ``config.selects.mode``.
                Under an overlay it keeps the pre-shim behaviour: the value is
                *stepped* with arrow keys on the collapsed control. An escape
                hatch must never be less visible than what shipped before.

        Without an overlay (compile) the value is set directly and nothing is
        animated — compilation is meant to be fast, not pretty — but the
        drivability of an enhanced widget is probed first, so an undriveable one
        surfaces here instead of after a multi-minute render.

        Raises:
            SelectDriveError: nothing visible could be clicked for this select.
        """

        # The readiness barrier: the classification pass decides whether this
        # select is shimmed, and every branch below asks that question.
        await self.frame.evaluate(_SELECTS_READY_JS)
        if self.overlay is None:
            locator = await self._point_and_prepare(target, click_sound=True)
            if not native:
                await self._probe_drivable(locator, option)
            # A select the page enhanced itself is routinely `display: none`
            # (Tom Select), which Playwright's actionability check would sit out
            # until it times out. Skipping the check for exactly those is what
            # makes spec §6's validation relaxation usable: without it a hidden
            # select would validate and then fail to compile.
            visible = await locator.is_visible()
            await locator.select_option(label=option, force=not visible)
            return
        if native:
            locator = await self._point_and_prepare(target, click_sound=True)
            await self._step_option_visibly(locator, option)
            return
        await self._select_in_two_beats(target, option)

    async def _describe(self, control: Locator | ElementHandle) -> str:
        """A short control name for an error message (``select#woj``)."""

        return await control.evaluate(_DESCRIBE_JS)

    async def _no_control_error(self, locator: Locator, option: str) -> SelectDriveError:
        return SelectDriveError(
            f"nie znaleziono widocznej kontrolki dla {await self._describe(locator)} — "
            f'nie da się pokazać na filmie wyboru opcji „{option}"'
        )

    async def _no_option_error(self, locator: Locator, option: str) -> SelectDriveError:
        return SelectDriveError(
            f"po rozwinięciu {await self._describe(locator)} nie pojawiła się "
            f'opcja „{option}" (limit {OPTION_WAIT_MS} ms)'
        )

    async def _probe_drivable(self, locator: Locator, option: str) -> None:
        """Fail at compile time for a widget the render could never drive.

        Only meaningful when the shim actually ran and declined to shim this
        select — that is what "the page enhanced it itself" looks like from
        here. With no shim installed there is nothing to conclude, and a shimmed
        select is drivable by construction.
        """

        state = await locator.evaluate(_SHIM_STATE_JS)
        if not state["installed"] or state["shimmed"]:
            return
        control = await associated_control(locator)
        if control is not None and await control.is_visible():
            return
        raise await self._no_control_error(locator, option)

    async def _select_in_two_beats(self, target: Target, option: str) -> None:
        """Open the list, then click the option — both with the cursor visible."""

        locator = await build_locator(self.frame, target)
        state = await locator.evaluate(_SHIM_STATE_JS)
        control: Locator | ElementHandle
        if state["shimmed"]:
            # The shim button is ``pointer-events: none``, so the <select> is the
            # hit target; its mousedown handler is what unfurls the DOM list.
            control = locator
        else:
            control = await self._page_widget(locator, option)
        # Beat 2 of the page-widget path recognises the option rows by "appeared
        # after the click", so the snapshot has to be taken before it.
        await self.frame.evaluate(_SNAPSHOT_JS)

        await self._approach(control, click_sound=True)  # beat 1
        await control.click()
        await self.page.wait_for_timeout(self.open_hold_ms)

        if state["shimmed"]:  # beat 2
            await self._click_shim_option(locator, option)
        else:
            await self._click_appeared_option(locator, option)

    async def _page_widget(self, locator: Locator, option: str) -> ElementHandle:
        """The visible control a page's own dropdown widget puts in the select's place."""

        control = await associated_control(locator)
        if control is None or not await control.is_visible():
            raise await self._no_control_error(locator, option)
        return control

    async def _click_shim_option(self, locator: Locator, option: str) -> None:
        """Beat 2 for a shimmed select: click the row the shim rendered."""

        # Read both *after* beat 1: the observer may have unshimmed and
        # reclassified the select while the list was opening (late select2
        # hydration), and then there is no row of ours to click.
        index = await locator.evaluate(
            "(el, label) => window.__guidebot_selects.optionIndexFor(el, label)", option
        )
        uid = await locator.get_attribute("data-guidebot-shimmed")
        if uid is None or index is None or index < 0:
            raise SelectDriveError(
                f'lista {await self._describe(locator)} nie zawiera opcji „{option}"'
            )
        # uid-scoped: the bare index attribute matches every shimmed select on the
        # page, which is a Playwright strict-mode violation.
        row = self.frame.locator(
            f'[data-guidebot-select-list][data-guidebot-for="{uid}"]'
            f' [data-guidebot-option-index="{index}"]'
        )
        try:
            await row.wait_for(state="visible", timeout=OPTION_WAIT_MS)
        except PlaywrightError as exc:
            raise await self._no_option_error(locator, option) from exc
        # Scroll the list *before* the glide: the cursor must travel to a row the
        # viewer can already see, not to one that scrolls under it on arrival.
        await locator.evaluate(
            "(el, i) => window.__guidebot_selects.scrollOptionIntoView(el, i)", index
        )
        await self._approach(row, click_sound=True)
        await row.click()

    async def _click_appeared_option(self, locator: Locator, option: str) -> None:
        """Beat 2 for a page's own widget: click the row it just rendered."""

        try:
            handle = await self.frame.wait_for_function(
                _APPEARED_NODE_JS, arg=option, timeout=OPTION_WAIT_MS
            )
        except PlaywrightError as exc:
            raise await self._no_option_error(locator, option) from exc
        row = handle.as_element()
        if row is None:
            await handle.dispose()
            raise await self._no_option_error(locator, option)
        # ``_approach`` scrolls the row into view on both axes, which is what
        # scrolls an internally-scrolling widget list to it.
        await self._approach(row, click_sound=True)
        await row.click()

    async def _step_option_visibly(self, locator: Locator, option: str) -> None:
        """The ``mode: native`` escape hatch: step the value with arrow keys.

        Retained verbatim from before the DOM shim. The collapsed control is all
        the viewer gets — the OS-drawn list cannot be filmed — but the value
        visibly walks to ``option`` instead of jumping, which is strictly more
        than a silent ``select_option`` would show.
        """

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
