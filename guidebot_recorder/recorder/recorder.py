"""Recorder — the Python API that drives the browser (§6).

The only place that "knows how": it builds a locator from the frozen `Target`
fields, animates the cursor (overlay), and performs the action via Playwright.
The overlay is optional — the `compile` phase needs no animation, so it can use
`Recorder(page, None)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from playwright.async_api import ElementHandle, Frame, Locator, Page
from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.chrome.typing import DEFAULT_MAX_DELAY_FACTOR, typing_schedule
from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.scenario import ResolvedHighlight, Scroll
from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.geometry import ellipse_around, fit_to_bounds
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.resolver.validate import build_locator
from guidebot_recorder.resolver.widget import associated_control
from guidebot_recorder.selects.selects import READY_TIMEOUT_MARKER, SelectsNotReadyError
from guidebot_recorder.selects.visibility import SELECT_SHAPE_JS

# WaitState → the state accepted by Playwright's locator.wait_for
_WAIT_STATE: dict[str, str] = {"visible": "visible", "hidden": "hidden", "enabled": "visible"}

#: How long the second beat waits for the option row to exist and be visible.
#: Generous, because a page widget may build its list asynchronously (or fetch
#: it), but bounded: a list that never appears must fail, not hang.
OPTION_WAIT_MS = 5000

#: :attr:`SelectDriveError.reason` — the control does not offer that option.
#:
#: The one cause a caller may legitimately answer with something other than
#: "fail": an ``optional: true`` step means "do this if it is on offer", and a
#: dropdown that no longer lists the label is exactly the absence that clause
#: describes. It is the same verdict ``validate.reuse_failure`` reports as
#: ``option_missing`` for a step it *does* get to preflight — an optional step
#: skips that preflight, so the miss can only surface from the drive itself.
OPTION_MISSING = "option_missing"

#: :attr:`SelectDriveError.reason` — anything else, and therefore a failure.
#:
#: Deliberately the default, so a raise site that says nothing is loud: a click
#: that did not take, a widget with nothing to unfurl, a shim taken off the
#: select mid-step and a row the page never rendered are all *broken steps*,
#: and a caller that quietly skipped them would hide the very failures this
#: choreography exists to surface.
UNDRIVABLE = "undrivable"


class SelectDriveError(RuntimeError):
    """A ``select:`` step could not be performed *visibly*.

    Raised when the choreography has nothing for the cursor to click: the page
    enhanced the ``<select>`` itself and the association heuristic found no
    visible control, the select is on screen but has no DOM option list to
    unfurl, or the list that did unfurl never produced a row matching the option
    label within :data:`OPTION_WAIT_MS`. The message names which of those it is,
    because the three want different fixes.

    There is deliberately no fallback to ``select_option()``. Falling back would
    restore exactly the invisible value change this choreography exists to
    remove, and would do it unobservably — the run would succeed and only a
    viewer would ever discover the step is unwatchable.

    ``reason`` splits "this control does not offer that option"
    (:data:`OPTION_MISSING`) from every other way the step can fail
    (:data:`UNDRIVABLE`). The split exists because those two answer the
    ``optional: true`` question differently and nothing else does: a label that
    is not on the list is the absence an optional step is allowed to shrug off,
    while a click that did not take, an undrivable widget, a shim removed
    mid-step and a row that never appeared all mean the step is broken however
    the author marked it. It is a machine-readable field rather than a message
    substring for the obvious reason — the messages are Polish prose written to
    be rewritten.

    The render layer catches this and re-raises it as a ``RenderError`` carrying
    the index of the failing step. ``RenderError`` lives in ``render.py``, which
    imports this module, so it cannot be raised from here without a cycle.
    """

    def __init__(self, message: str, *, reason: str = UNDRIVABLE) -> None:
        super().__init__(message)
        self.reason = reason


#: (el) => {installed, shimmed, listbox, hidden, markerClass} — how this select
#: is presented.
#:
#: ``installed`` distinguishes "the widget ran and decided not to shim this
#: select" (so the page enhanced it itself, or ``mode: native`` is in force)
#: from "no shim layer here at all" — a bare context such as a health probe or
#: a unit-test page. Only the former says anything about drivability.
#:
#: ``listbox`` is the shim's own non-goal, read back here: ``multiple`` and
#: ``size > 1`` render their option list in the page already, so the shim never
#: touches them — which is precisely why they need their own path rather than
#: the page-widget one.
#:
#: ``hidden`` and ``markerClass`` are the two halves of the *shared* predicate
#: (``selects/visibility.js``), embedded rather than restated: ``hidden`` tells
#: "the page replaced this control" apart from "the control is on screen but
#: carries no DOM list", and ``markerClass`` names the class that caused the
#: latter, so the error message can say which one it was.
_SHIM_STATE_JS = f"""(el) => {{
  const api = window.__guidebot_selects;
  const shape = ({SELECT_SHAPE_JS})(el);
  return {{
    installed: !!api,
    shimmed: !!(api && api.isShimmed(el)),
    listbox: !!el.multiple || el.size > 1,
    hidden: !shape.visible,
    markerClass: shape.markerClass,
  }};
}}"""

#: (el) => string | null — the label of the option currently selected.
#:
#: Normalised exactly the way ``optionLabel`` normalises it in ``selects.js``,
#: which is in turn the rule Playwright's ``select_option(label=…)`` applies:
#: the ``label`` attribute when present, the option's text otherwise. Anything
#: else would let the read-back disagree with the write it is verifying.
_SELECTED_LABEL_JS = """(el) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const option = el.selectedOptions[0];
  if (!option) return null;
  return norm(option.label ? option.label : option.textContent);
}"""

#: (el, label) => number — index of the first ``<option>`` carrying ``label``.
#:
#: ``HTMLOptionElement.label`` is the ``label`` attribute when present and the
#: trimmed text otherwise, which is the same rule Playwright's
#: ``select_option(label=…)`` applies — so the option this finds is the option
#: the direct path would have set. ``el.options`` is the flattened, document
#: order list, so the index also addresses ``locator("option").nth(i)`` even
#: across ``<optgroup>`` boundaries. ``-1`` when no option matches.
_OPTION_INDEX_JS = """(el, label) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const wanted = norm(label);
  for (let i = 0; i < el.options.length; i += 1) {
    if (norm(el.options[i].label) === wanted) return i;
  }
  return -1;
}"""

#: (el) => void — hand this select back to the browser, durably.
#:
#: The per-step ``mode: native`` override exists for one stubborn widget in an
#: otherwise shimmed scenario, so under a global ``shim`` the select it names is
#: already shimmed: its button and DOM list sit visually on top of the real
#: control, so the cursor's approach must land on the genuine, unshimmed select —
#: otherwise the ripple would target a widget that is about to disappear out from
#: under it. Absent (bare context, ``mode: native`` globally) this is a no-op:
#: there is no shim to undo.
_PIN_NATIVE_JS = """(el) => {
  const api = window.__guidebot_selects;
  if (api && api.pinNative) {
    api.pinNative(el);
  }
}"""

#: Backstop bound, in milliseconds, on :meth:`Recorder._await_selects_ready`.
#:
#: Not the primary barrier — compile and render both take the bounded
#: :meth:`guidebot_recorder.selects.Selects.wait_ready`, whose deadline tracks
#: ``settle_ms``, long before a step reaches the recorder. This is the floor
#: under a *direct* caller, which nothing near this module can force through
#: that barrier first, so it is a flat, generous constant rather than something
#: derived from a config the recorder is never handed.
READY_WAIT_MS = 15_000

#: The readiness barrier of spec §3, read straight off the page.
#:
#: Deliberately not routed through :class:`guidebot_recorder.selects.Selects`:
#: the recorder is handed a page, not the controller that installed the widget,
#: and a missing API must degrade to "nothing to wait for", not to an error.
#:
#: Bounded by the same page-side ``Promise.race`` idiom ``Selects.wait_ready``
#: uses, rejecting with the same marker: the barrier is a promise the *page*
#: settles, so awaiting it bare makes a wedged page hang the caller — precisely
#: the failure that barrier exists to prevent.
#:
#: ``settled()`` rather than ``ready``, for the reason ``Selects.wait_ready``
#: gives: ``ready`` reports the *first* classification pass and never re-arms,
#: so a select the page appended a moment ago is still unclassified when it
#: resolves — and this method is the last barrier before the step drives that
#: select. ``ready`` remains the fallback for a partial API object.
_SELECTS_READY_JS = f"""(timeoutMs) => {{
  const api = window.__guidebot_selects;
  if (!api || !api.ready) return null;
  const barrier = typeof api.settled === "function" ? api.settled() : api.ready;
  return Promise.race([
    barrier,
    new Promise((_resolve, reject) => {{
      window.setTimeout(() => reject(new Error({READY_TIMEOUT_MARKER!r})), timeoutMs);
    }}),
  ]);
}}"""

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
#:
#: A ``WeakSet``, not a ``Set``: this is parked on ``window`` until the next
#: ``select:`` step overwrites it, and a strong set of every element in the
#: document would keep every node the page detaches in the meantime alive with
#: it. Membership is all this is ever asked, and ``WeakSet`` answers that.
_SNAPSHOT_JS = """() => {
  window.__guidebot_select_snapshot = new WeakSet(document.querySelectorAll("*"));
}"""

#: () => boolean — is the pre-click snapshot still on this document?
_HAS_SNAPSHOT_JS = "() => !!window.__guidebot_select_snapshot"

#: (label) => Element | null — the first *newly added* visible element whose
#: trimmed text is exactly the option label. ``querySelectorAll`` yields
#: document order, so the first hit is the document-order tie-break of spec §4.
#:
#: A missing snapshot yields ``null``, never a match. It used to be spelled
#: ``if (seen && seen.has(node))``, which turned the "appeared after" filter
#: into a no-op the moment beat 1 replaced the document — every node on the page
#: then qualified, up to and including ``<html>`` itself. The caller checks for
#: the snapshot explicitly and says so; this is the second lock on the same door.
_APPEARED_NODE_JS = """(label) => {
  const seen = window.__guidebot_select_snapshot;
  if (!seen) return null;
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const wanted = norm(label);
  for (const node of document.querySelectorAll("*")) {
    if (seen.has(node)) continue;
    if (norm(node.textContent) !== wanted) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    return node;
  }
  return null;
}"""


class PointResult(NamedTuple):
    locator: Locator
    box: dict | None
    center: tuple[float, float] | None


class SelectReveal(NamedTuple):
    """Where a ``select:`` step's marks belong, at the instant before the choice.

    ``control_*`` is the control the viewer points at: the ``<select>`` itself
    when the shim owns it, the page's own widget when the page took it over, the
    listbox when it draws its own rows. ``row_*`` is the option row that is about
    to be clicked.

    ``row_*`` is ``None`` exactly when nothing was unfurled — ``mode: native``,
    where the option list is an OS popup no screenshot can hold, or a compile run
    with no overlay at all. A consumer reads that as "there is no row to mark".

    Boxes are Playwright bounding boxes in viewport pixels — the same units
    :meth:`Recorder.point` already hands the PDF guide.
    """

    control_box: dict | None
    control_center: tuple[float, float] | None
    row_box: dict | None = None
    row_center: tuple[float, float] | None = None


#: Awaited by :meth:`Recorder.select` while the option list is open.
RevealHook = Callable[[SelectReveal], Awaitable[None]]


def _center_of(box: dict | None) -> tuple[float, float] | None:
    """Centre of a Playwright bounding box, or ``None`` when there is no box."""

    if box is None:
        return None
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


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
            center = _center_of(box)
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

        A native select draws its option list as an OS popup, which the
        screencast cannot record; ``selects.js`` therefore replaces that list
        with a DOM one, and this method drives it in two visible beats (spec
        §4): the cursor glides to the control and clicks it, the list unfurls,
        then the cursor glides to the wanted row and clicks that. Pages that
        enhance their own selects (select2, Tom Select) already have a DOM list;
        those are driven the same way, only the two click targets differ.

        A ``multiple`` / ``size > 1`` select is the third shape and gets its own,
        single-beat path (:meth:`_select_in_listbox`): its option list is already
        laid out in the page, so there is no list to unfurl and nothing for the
        shim to stand in for — the cursor simply travels to the ``<option>`` and
        clicks it.

        Args:
            target: the frozen target of the ``<select>`` itself — never of the
                widget standing in for it, which no ``Target`` names.
            option: the visible label to choose.
            native: the ``mode: native`` escape hatch, already resolved by the
                caller from the per-step override and ``config.selects.mode``.
                The cursor travels to the collapsed control, ripples, and the
                value is set directly — the option list itself is never shown,
                because a native ``<select>`` draws it as an OS popup no
                browser-automation tool can screenshot. Under a global ``shim``
                the select it names is already shimmed, so this first hands
                that one control back to the browser (see :data:`_PIN_NATIVE_JS`)
                — otherwise the cursor would be landing on a widget the hatch
                just opted out of.
            ripple: draw the click ring (and its flash) when the cursor lands.
                The PDF guide turns it off: a still capture wants a clean frame,
                and the ring would be frozen mid-animation in it.
            on_revealed: awaited **exactly once, immediately before the click (or
                ``select_option``) that commits the choice**, on every path. That
                instant is the only one at which the list is open, the cursor is
                on the option row and nothing has been chosen yet — which is
                precisely the frame the PDF guide has to keep. It is handed a
                :class:`SelectReveal`; a hook that raises aborts the step with
                the choice not made.

        Without an overlay (compile) the value is set directly and nothing is
        animated — compilation is meant to be fast, not pretty — but an enhanced
        widget is probed first (:meth:`_probe_drivable`), so one with *nothing to
        click* surfaces here instead of after a multi-minute render. That probe
        is narrower than "drivable"; see its docstring for what it cannot see.

        Raises:
            SelectDriveError: nothing visible could be clicked for this select,
                or the option is not on offer — :attr:`SelectDriveError.reason`
                tells the two apart on every path, direct ones included.
            SelectsNotReadyError: the widget is installed in this frame but its
                first classification pass never finished (see
                :data:`READY_WAIT_MS`).
        """

        # The readiness barrier: the classification pass decides whether this
        # select is shimmed, and every branch below asks that question.
        await self._await_selects_ready()
        if self.overlay is None:
            locator = await self._point_and_prepare(target, click_sound=True)
            if native:
                await self._pin_native(locator)
            else:
                await self._probe_drivable(locator, option)
            await self._require_option(locator, option)
            await self._reveal(on_revealed, SelectReveal(None, None))
            await self._set_option_directly(locator, option)
            return
        if native:
            locator = await build_locator(self.frame, target)
            # Before the cursor sets off, not after: the shim's button and list
            # must be gone before this control is on camera, or the ripple would
            # land on a widget that vanishes out from under it.
            await self._pin_native(locator)
            # Before the cursor sets off, too: a label this select does not carry
            # is the one refusal a caller may answer with a skip, and it should
            # not first cost a glide and a ripple towards a choice that cannot be
            # made.
            await self._require_option(locator, option)
            box, center = await self._approach(locator, ripple=ripple, click_sound=True)
            # No row geometry: `native` never unfurls anything, so a still
            # capture can only show the collapsed control.
            await self._reveal(on_revealed, SelectReveal(box, center))
            await self._set_option_directly(locator, option)
            return
        locator = await build_locator(self.frame, target)
        # Which of the three shapes this is, read from the DOM once and passed
        # down. "Not shimmed" is not on its own evidence of a page widget: the
        # shim also declines a natively-visible listbox, and that one has its
        # own single-beat path rather than an association heuristic to run.
        state = await locator.evaluate(_SHIM_STATE_JS)
        if state["listbox"]:
            await self._select_in_listbox(locator, option, ripple=ripple, on_revealed=on_revealed)
            return
        await self._select_in_two_beats(
            locator, state, option, ripple=ripple, on_revealed=on_revealed
        )

    @staticmethod
    async def _reveal(hook: RevealHook | None, reveal: SelectReveal) -> None:
        """Hand the caller the open list's geometry, before anything is chosen."""

        if hook is not None:
            await hook(reveal)

    async def _await_selects_ready(self) -> None:
        """Wait until this frame owes no classification pass — but not forever.

        Both production callers (compile, render) take
        :meth:`guidebot_recorder.selects.Selects.wait_ready` first, so this
        normally finds a barrier that has already settled. It is written as a
        bound anyway because that ordering is an invariant of *those* call
        sites, not of this one: a direct caller on a page whose widget is wedged
        would otherwise get an ``evaluate`` that never returns, which is the one
        outcome the barrier design rules out everywhere else.

        Not merely "the first pass has run": every branch below reads whether
        *this* select is shimmed, and a select the previous step added is still
        unclassified while the debounced pass it triggered is pending. See
        :data:`_SELECTS_READY_JS`.

        Raises:
            SelectsNotReadyError: the widget is in this frame and its barrier
                did not settle within :data:`READY_WAIT_MS`.
        """

        try:
            # The page-side race is the primary guard; the outer wait covers a
            # document that has stopped running timers at all.
            await asyncio.wait_for(
                self.frame.evaluate(_SELECTS_READY_JS, READY_WAIT_MS),
                timeout=READY_WAIT_MS / 1000 + 1.0,
            )
        except TimeoutError as exc:
            raise self._not_ready_error() from exc
        except PlaywrightError as exc:
            if READY_TIMEOUT_MARKER not in str(exc):
                raise
            raise self._not_ready_error() from exc

    def _not_ready_error(self) -> SelectsNotReadyError:
        """Phrased like ``Selects._not_ready``: same failure, same two fixes."""

        return SelectsNotReadyError(
            f"widget select nie zgłosił gotowości w ciągu {READY_WAIT_MS / 1000:.1f} s "
            f"dla ramki {getattr(self.frame, 'url', '') or '(nieznany adres)'}. "
            f"Zwiększ selects.settleMs, jeśli strona długo się inicjalizuje, albo "
            f"ustaw selects.mode: native, aby zrezygnować z podmiany list "
            f"rozwijanych na tej stronie."
        )

    async def _pin_native(self, locator: Locator) -> None:
        """Drop the shim from this select and keep it off (see :data:`_PIN_NATIVE_JS`)."""

        await locator.evaluate(_PIN_NATIVE_JS)

    async def _describe(self, control: Locator | ElementHandle) -> str:
        """A short control name for an error message (``select#woj``)."""

        return await control.evaluate(_DESCRIBE_JS)

    async def _no_control_error(
        self, locator: Locator, option: str, state: dict
    ) -> SelectDriveError:
        """Name the situation the select is actually in, not just the empty result.

        Three different pages end up here and they need three different fixes,
        so the message must not collapse them:

        * the page hid the ``<select>`` and the association heuristic found
          nothing standing in for it — a widget library that failed to
          initialise, or one whose control loads over the network;
        * the select is on screen and there is no shim layer in this context at
          all, which is ``config.selects.mode: native`` (or a bare context such
          as a health probe). Nothing is going to unfurl, ever;
        * the select is on screen, the shim ran, and it declined this one —
          a marker class it honours, or ``mode: native`` pinned onto it.

        The middle case used to be unreachable: the caller sent it into the
        association heuristic, which always finds *something*, so the run
        clicked an unrelated sibling on camera and then blamed the option.
        """

        # Medium-neutral wording: the PDF guide raises these too, and a message
        # about "the film" reaches an author who asked for a document.
        tail = f'nie da się pokazać rozwiniętej listy z opcją „{option}"'
        described = await self._describe(locator)
        if state["hidden"]:
            return SelectDriveError(
                f"strona ukryła {described} i nie znaleziono widocznej kontrolki, "
                f"która ją zastępuje — {tail}"
            )
        if not state["installed"]:
            return SelectDriveError(
                f"{described} jest widoczna, ale w tym kontekście nie zainstalowano "
                f"nakładki select, więc nie ma czego rozwinąć — {tail}. Najczęstsza "
                f"przyczyna to `config.selects.mode: native` (wtedy skrypt nakładki "
                f"w ogóle nie jest wstrzykiwany) — użyj `config.selects.mode: shim` "
                f"albo `mode: native` na tym kroku"
            )
        marker = state["markerClass"]
        cause = (
            f"nakładka pominęła ją z powodu klasy `{marker}`"
            if marker
            else "nakładka jej nie objęła"
        )
        return SelectDriveError(
            f"{described} jest widoczna, ale nie ma listy opcji w DOM — {cause}, "
            f"a listę natywnego selecta rysuje system operacyjny; {tail} "
            f"(użyj `mode: native`, jeśli sam wybór wystarczy)"
        )

    async def diagnose_select(self, target: Target, option: str) -> SelectDriveError:
        """Why this ``<select>`` cannot be revealed, phrased for the author.

        The public face of :meth:`_no_control_error`, for a caller that has
        already learned *that* a select is undrivable from somewhere else and now
        wants the situation named. The PDF guide's preflight is that caller: its
        reuse check answers ``not_visible``, which for a ``select`` action has
        exactly one cause — ``validate_compile_time``'s select arm reaches it
        only through ``user_visible_control() is None`` — but says so in a
        sentence shared with ``click``, ``hover`` and ``type``. Rather than
        write a second wording for the guide, it asks here and raises what the
        render would have raised.

        Returns the error rather than raising it so the caller can wrap it in
        its own step banner (`plik:linia` plus the YAML fragment) without having
        to catch what it just constructed.
        """

        locator = await build_locator(self.frame, target)
        state = await locator.evaluate(_SHIM_STATE_JS)
        return await self._no_control_error(locator, option, state)

    async def _option_missing_error(self, locator: Locator, option: str) -> SelectDriveError:
        """This ``<select>`` does not carry that label — the one skippable cause.

        The only refusal a caller is allowed to answer with anything other than
        "fail" (see :data:`OPTION_MISSING`), so it is built in one place and
        every path that can establish the fact routes through here — including
        the two direct ones, which would otherwise leave the miss to
        ``select_option``'s actionability timeout and report it as a Playwright
        error nobody can classify.
        """

        return SelectDriveError(
            f'lista {await self._describe(locator)} nie zawiera opcji „{option}"',
            reason=OPTION_MISSING,
        )

    async def _require_option(self, locator: Locator, option: str) -> None:
        """Refuse up front when the select does not offer ``option``.

        For the two paths that never unfurl a list — compile's direct set and
        ``mode: native`` — this is the whole of "is the option there?". The
        on-camera paths learn the same thing from the list they opened, so all
        four classify a vanished option identically; without it the direct paths
        would answer a caller's ``optional:`` question with an unclassifiable
        Playwright timeout instead.

        :data:`_OPTION_INDEX_JS` applies the same ``HTMLOptionElement.label``
        rule Playwright's ``select_option(label=…)`` matches on, so this and the
        call it guards agree about which labels exist.
        """

        if await locator.evaluate(_OPTION_INDEX_JS, option) < 0:
            raise await self._option_missing_error(locator, option)

    @staticmethod
    async def _set_option_directly(locator: Locator, option: str) -> None:
        """Set the value with no list involved, for a control that may be hidden.

        A select the page enhanced itself is routinely ``display: none`` (Tom
        Select), which Playwright's actionability check would sit out until it
        times out. Skipping the check for exactly those is what makes spec §6's
        validation relaxation usable: without it a hidden select would validate
        and then fail to compile.
        """

        visible = await locator.is_visible()
        await locator.select_option(label=option, force=not visible)

    async def _no_option_error(self, locator: Locator, option: str) -> SelectDriveError:
        """The list unfurled but the row never turned up — cause unestablished.

        Deliberately *not* :data:`OPTION_MISSING`. On the shimmed path this is
        only reached with the option's index already in hand, so the label is
        demonstrably there and it is the rendering that failed; on the page's own
        widget the caller has already checked the underlying ``<select>``, so a
        row that still does not appear means the widget did not draw it. Either
        way the step is broken, and a caller must not shrug it off as "the option
        was not on offer".
        """

        return SelectDriveError(
            f"po rozwinięciu {await self._describe(locator)} nie pojawiła się "
            f'opcja „{option}" (limit {OPTION_WAIT_MS} ms)'
        )

    async def _unshimmed_mid_step_error(self, locator: Locator, option: str) -> SelectDriveError:
        """Beat 1 opened our list; by beat 2 the shim was no longer on this select.

        The classification pass runs on every mutation, so a page that enhances
        a select on first interaction (select2 hydrating on ``mousedown``) can
        legitimately take the control over between the two beats: the marker
        class appears, the observer unshims, and the rows the second beat was
        about to click are gone with it.

        Named as its own failure because the fix is not the one every other
        beat-2 message points at. The option label is fine — the recording just
        raced the page's own widget, and the answer is a longer
        ``selects.openHoldMs`` (or ``settleMs``), or ``mode: native`` for that
        step, not a corrected label.
        """

        state = await locator.evaluate(_SHIM_STATE_JS)
        marker = state["markerClass"]
        because = (
            f"strona przejęła kontrolkę (klasa `{marker}`)"
            if marker
            else "strona zmieniła kontrolkę"
        )
        return SelectDriveError(
            f"nakładka nad {await self._describe(locator)} zniknęła w trakcie kroku "
            f"— {because} już po rozwinięciu listy, więc nie ma czego kliknąć dla "
            f'opcji „{option}". Zwiększ selects.openHoldMs albo selects.settleMs, '
            f"aby strona zdążyła się ulepszyć przed krokiem, albo ustaw dla tego "
            f"kroku mode: native"
        )

    async def _confirm_selected(self, locator: Locator, option: str) -> None:
        """Read the select back after the click, and fail if it did not take.

        The whole branch exists so a run that would produce an unwatchable video
        fails loudly rather than succeeding quietly. Every on-camera path ends
        at ``row.click()``, and a click is not evidence: a disabled row refuses
        it, a page widget can hand back a decoy node carrying the same label, and
        a page can cancel the event outright. In all three the value never
        changes and there is no exception anywhere to notice — unlike compile's
        direct path, where ``select_option`` throws.

        So the last thing every path does is ask the ``<select>`` what it is
        actually showing. That is the only observation that means the step is
        watchable.
        """

        # ``" ".join(split())`` is the Python spelling of the page's own
        # ``replace(/\s+/g, " ").trim()``, so the two sides of the comparison
        # are normalised by the same rule.
        actual = await locator.evaluate(_SELECTED_LABEL_JS)
        if actual == " ".join(option.split()):
            return
        described = await self._describe(locator)
        shown = f'„{actual}"' if actual is not None else "nic"
        raise SelectDriveError(
            f'kliknięcie opcji „{option}" w {described} nie zmieniło wyboru — '
            f"wybrane jest {shown}. Opcja mogła być wyłączona (`disabled`), "
            f"albo kursor trafił w element, który tylko powtarza tę etykietę"
        )

    async def _probe_drivable(self, locator: Locator, option: str) -> None:
        """Fail at compile time for a widget the render could never drive.

        Only meaningful when the shim actually ran and declined to shim this
        select *because the page took it over* — that is what "the page enhanced
        it itself" looks like from here. With no shim installed there is nothing
        to conclude, and a shimmed select is drivable by construction.

        "Not shimmed" is not on its own evidence of a page widget, though: the
        shim also declines a ``multiple`` / ``size > 1`` select, which needs no
        stand-in because its option list is laid out in the page already. Reading
        that as "the page must have enhanced this" is what used to send a
        perfectly filmable listbox into the association heuristic and fail the
        compile with a missing-widget error.

        **What this does not catch.** It asks whether
        :func:`associated_control` resolves *a visible element*, not whether that
        element is the right one. The heuristic's last step is "nearest following
        sibling with a box" (``resolver/widget.py``), so a hidden select whose
        real widget sits elsewhere in the document can be blessed by an unrelated
        neighbour. Compile passes regardless — its value-set goes through
        ``select_option``, never through the widget — and the render is where it
        shows: the cursor clicks that neighbour on camera and beat 2 waits for a
        row that never appears. Deciding the *wrong control* case here would mean
        opening the widget and inspecting what came up, which is the render
        choreography itself; the honest boundary is "nothing to click", not
        "undrivable".
        """

        state = await locator.evaluate(_SHIM_STATE_JS)
        if not state["installed"] or state["shimmed"] or state["listbox"]:
            return
        control = await associated_control(locator)
        if control is None:
            raise await self._no_control_error(locator, option, state)
        try:
            drivable = await control.is_visible()
        finally:
            # The probe only ever asked a yes/no question, so the handle is
            # released either way (see the ownership note on
            # `associated_control`); compile runs this once per `select:` step.
            await control.dispose()
        if drivable:
            return
        raise await self._no_control_error(locator, option, state)

    async def _commit_option(
        self,
        select: Locator,
        row: Locator | ElementHandle,
        option: str,
        *,
        ripple: bool,
        on_revealed: RevealHook | None,
        control_box: dict | None,
        control_center: tuple[float, float] | None,
    ) -> None:
        """The tail all three on-camera paths share: land on the row, then choose.

        Written once because the order inside it is load-bearing and the three
        paths must not be free to disagree about it: the cursor arrives, the
        caller gets its one look at the open list, and only then does the click
        that closes the list and changes the value happen. Reading the select
        back afterwards is what turns a click that landed on nothing into a
        failure instead of a quietly wrong recording.
        """

        box, center = await self._approach(row, ripple=ripple, click_sound=True)
        await self._reveal(on_revealed, SelectReveal(control_box, control_center, box, center))
        await row.click()
        await self._confirm_selected(select, option)

    async def _select_in_listbox(
        self,
        locator: Locator,
        option: str,
        *,
        ripple: bool = True,
        on_revealed: RevealHook | None = None,
    ) -> None:
        """One visible beat for a select that renders its own in-page listbox.

        ``multiple`` and ``size > 1`` are the shim's documented non-goal — they
        draw no OS popup, so there is nothing to replace — but that also means
        their ``<option>`` elements have real layout and are on screen from the
        start. So the interaction is filmed for real rather than merely
        approached: the cursor glides to the row and clicks it, exactly once.

        Measured with this repo's pinned Playwright (Chromium 149, headless and
        headed): a plain left click on an ``<option>`` inside such a select does
        select it and does fire ``change``, and ``scrollIntoView`` on the option
        scrolls the listbox's own viewport, so the cursor lands on a row the
        viewer can already see (:meth:`_approach` does that scroll).

        The click's effect on *other* options is the same as the direct path's:
        an unmodified click replaces the whole selection, and so does
        ``select_option(label=…)``. A ``select:`` step has always meant "this one
        option is now chosen", and it still does.
        """

        index = await locator.evaluate(_OPTION_INDEX_JS, option)
        if index < 0:
            raise await self._option_missing_error(locator, option)
        row = locator.locator("option").nth(index)
        # The listbox *is* the control the viewer sees, so its own box is the one
        # a still capture frames. Reading it moves no cursor and opens nothing.
        control_box = await locator.bounding_box()
        await self._commit_option(
            locator,
            row,
            option,
            ripple=ripple,
            on_revealed=on_revealed,
            control_box=control_box,
            control_center=_center_of(control_box),
        )

    async def _select_in_two_beats(
        self,
        locator: Locator,
        state: dict,
        option: str,
        *,
        ripple: bool = True,
        on_revealed: RevealHook | None = None,
    ) -> None:
        """Open the list, then click the option — both with the cursor visible."""

        control: Locator | ElementHandle
        if state["shimmed"]:
            # The shim button is ``pointer-events: none``, so the <select> is the
            # hit target; its mousedown handler is what unfurls the DOM list.
            control = locator
        elif not state["installed"] and not state["hidden"]:
            # A visible select in a context with no shim layer. "Not shimmed" is
            # not evidence of a page widget here — there is nothing that *could*
            # have shimmed it. Sending this to the association heuristic makes it
            # click whatever sits next to the select, on camera, and then blame
            # the option for not appearing.
            raise await self._no_control_error(locator, option, state)
        else:
            control = await self._page_widget(locator, state, option)
        try:
            # Beat 2 of the page-widget path recognises the option rows by
            # "appeared after the click", so the snapshot has to be taken before
            # it.
            await self.frame.evaluate(_SNAPSHOT_JS)

            control_box, control_center = await self._approach(  # beat 1
                control, ripple=ripple, click_sound=True
            )
            await control.click()
            await self.page.wait_for_timeout(self.open_hold_ms)

            row: Locator | ElementHandle
            if state["shimmed"]:  # beat 2
                row = await self._shim_option_row(locator, option)
            else:
                row = await self._appeared_option_row(locator, option)
            try:
                await self._commit_option(
                    locator,
                    row,
                    option,
                    ripple=ripple,
                    on_revealed=on_revealed,
                    control_box=control_box,
                    control_center=control_center,
                )
            finally:
                # A handle on the page-widget path; a `Locator` on the shimmed
                # one, which owns nothing to release.
                if isinstance(row, ElementHandle):
                    await row.dispose()
        finally:
            # `control` is a `Locator` on the shimmed path (nothing to release)
            # and a handle on the page-widget one, which this frame owns from
            # here (see the ownership note on `associated_control`).
            if isinstance(control, ElementHandle):
                await control.dispose()

    async def _page_widget(self, locator: Locator, state: dict, option: str) -> ElementHandle:
        """The visible control a page's own dropdown widget puts in the select's place."""

        control = await associated_control(locator)
        if control is None:
            raise await self._no_control_error(locator, option, state)
        if not await control.is_visible():
            await control.dispose()
            raise await self._no_control_error(locator, option, state)
        return control

    async def _shim_option_row(self, locator: Locator, option: str) -> Locator:
        """Beat 2 for a shimmed select: the row the shim rendered, ready to click.

        Everything that has to be true *before* the cursor sets off happens here
        — the row exists, is visible, is not ``disabled``, and the list has been
        scrolled to it — and nothing that changes the page's state does. Handing
        the row back rather than clicking it is what lets the PDF guide take its
        frame in between; the click itself is :meth:`_commit_option`'s.
        """

        # Read both *after* beat 1: the observer may have unshimmed and
        # reclassified the select while the list was opening (late select2
        # hydration), and then there is no row of ours to click.
        index = await locator.evaluate(
            "(el, label) => window.__guidebot_selects.optionIndexFor(el, label)", option
        )
        uid = await locator.get_attribute("data-guidebot-shimmed")
        # Two different events, so two different messages. A missing `uid` means
        # the shim was taken off this select between the beats — the option is
        # very probably still there, and blaming the list for "not containing"
        # it sends the author hunting for a typo in a label spelled perfectly.
        if uid is None:
            raise await self._unshimmed_mid_step_error(locator, option)
        if index is None or index < 0:
            raise await self._option_missing_error(locator, option)
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
        # Visible is not the same as clickable. A disabled row is rendered — at
        # `opacity: .45`, so a viewer can read it — and both `onListClick` and
        # `choose` return early for it. Clicking it would be a no-op the run has
        # no other way to notice, and sending the cursor there at all would film
        # a choice the page was never going to accept.
        if await row.get_attribute("data-guidebot-option-disabled") is not None:
            raise SelectDriveError(
                f'opcja „{option}" na liście {await self._describe(locator)} jest '
                f"wyłączona (`disabled`) — nie da się jej wybrać ani z rozwiniętej "
                f"listy, ani bezpośrednio"
            )
        # Scroll the list *before* the glide: the cursor must travel to a row the
        # viewer can already see, not to one that scrolls under it on arrival.
        await locator.evaluate(
            "(el, i) => window.__guidebot_selects.scrollOptionIntoView(el, i)", index
        )
        return row

    async def _appeared_option_row(self, locator: Locator, option: str) -> ElementHandle:
        """Beat 2 for a page's own widget: the row it just rendered.

        "The row it just rendered" is defined entirely by the snapshot taken
        before beat 1, so a missing snapshot is not a degraded search — it is no
        search at all, and must be said out loud rather than answered with the
        first node on the page that happens to carry the label.

        The underlying ``<select>`` is consulted before the wait, because it is
        the only thing here that can tell "the option is not on offer" from "the
        widget did not draw a row", and those two want different answers from a
        caller (:data:`OPTION_MISSING`). A page widget keeps the original's
        ``<option>`` elements — that is what it submits — so the same question
        ``validate.reuse_failure`` asks at preflight is answerable here. It also
        saves waiting :data:`OPTION_WAIT_MS` for a row that could not exist.
        """

        if not await self.frame.evaluate(_HAS_SNAPSHOT_JS):
            raise SelectDriveError(
                f"stan sprzed rozwinięcia listy {await self._describe(locator)} zniknął "
                f"— dokument został podmieniony w trakcie kroku, więc nie da się odróżnić "
                f"świeżo narysowanych opcji od reszty strony; nie da się pokazać "
                f'rozwiniętej listy z opcją „{option}"'
            )
        await self._require_option(locator, option)
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
        # ``_commit_option``'s approach scrolls the row into view on both axes,
        # which is what scrolls an internally-scrolling widget list to it.
        return row

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
