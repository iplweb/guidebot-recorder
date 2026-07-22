"""The visible choreography of a ``select:`` step — the only stateful part.

A native ``<select>`` draws its option list as an OS popup, which no screencast
can record. ``selects.js`` replaces that list with a DOM one; this module drives
it, and the two page shapes that need no shim, in the beats a viewer can follow.

**Why this is a class and the rest of the package is functions.** Everything in
:mod:`~guidebot_recorder.recorder.select.probe` answers a question about one
locator and forgets it. This holds five things for the length of a step — the
page, the frame, how to glide the cursor somewhere, whether anything is being
animated at all, and how long to hold the open list — and every method needs
several of them.

**What it is handed, and what it is deliberately not.** The constructor takes
those five as named parameters instead of a reference back to the
:class:`~guidebot_recorder.recorder.recorder.Recorder` that owns them. A
back-reference would be the same god-class with one extra hop: ``self._rec.page``
hides that this code touches exactly one page method, and ``self._rec.overlay``
hides that it never touches the overlay at all — it only ever asks whether one
exists, which is what ``animated`` names outright.

``approach`` is a late-bound callable rather than a bound method, and the tests
depend on it: ``test_recorder_select.py`` replaces ``_approach`` **on a Recorder
instance** to sample the option list's geometry on both sides of every cursor
glide. A bound method captured when this object is built freezes the original,
so the spy would see nothing and the assertion that the list is scrolled
*before* the cursor sets off would pass while testing nothing. The recorder
therefore hands over ``lambda *a, **kw: self._approach(*a, **kw)``, which
resolves the attribute at the moment of the call — verified by mutation: hoist
the driver into ``Recorder.__init__`` and swap the lambda for a bound method,
and that test drops from two sampled glides to zero.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple, Protocol

from playwright.async_api import ElementHandle, Frame, Locator, Page
from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.models.target import Target
from guidebot_recorder.overlay.geometry import center_of
from guidebot_recorder.recorder.select import _js, probe
from guidebot_recorder.recorder.select.errors import (
    SelectDriveError,
    disabled_option_error,
    hidden_listbox_error,
    no_control_error,
    no_option_error,
    option_missing_error,
    snapshot_lost_error,
    unshimmed_mid_step_error,
)
from guidebot_recorder.resolver.validate import build_locator
from guidebot_recorder.resolver.widget import associated_control

#: How long the second beat waits for the option row to exist and be visible.
#: Generous, because a page widget may build its list asynchronously (or fetch
#: it), but bounded: a list that never appears must fail, not hang.
#:
#: A test seam: the suite shrinks it so a "the row never appeared" test costs
#: 0.4 s instead of 5. Nothing may ``from … import`` it — a copy in another
#: module's globals would never see the patch, and the only symptom would be a
#: slow suite. :func:`~...select.errors.no_option_error` is *handed* the value
#: for the same reason, so the limit it quotes is the limit that elapsed.
OPTION_WAIT_MS = 5000


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
    :meth:`~guidebot_recorder.recorder.recorder.Recorder.point` already hands the
    PDF guide.
    """

    control_box: dict | None
    control_center: tuple[float, float] | None
    row_box: dict | None = None
    row_center: tuple[float, float] | None = None


#: Awaited by :meth:`SelectDriver.select` while the option list is open.
RevealHook = Callable[[SelectReveal], Awaitable[None]]


class Approach(Protocol):
    """Glide the cursor onto a node, ripple, settle — and report where it landed.

    A ``Protocol`` rather than a bare ``Callable`` because the keyword arguments
    carry the meaning: ``ripple`` is the click ring a still capture wants
    suppressed, ``click_sound`` is the SFX beat. Spelling them out here is what
    lets this module state its dependency on the recorder as a signature instead
    of as a reference to the recorder.
    """

    async def __call__(
        self,
        control: Locator | ElementHandle,
        *,
        ripple: bool = True,
        click_sound: bool = False,
    ) -> tuple[dict | None, tuple[float, float] | None]: ...


class SelectDriver:
    """Drives one ``select:`` step, in whichever of the three shapes the page has."""

    def __init__(
        self,
        *,
        page: Page,
        frame: Page | Frame,
        approach: Approach,
        animated: bool,
        open_hold_ms: float,
    ) -> None:
        # The page, not the frame: the only thing asked of it is the pause that
        # holds the open list on screen, which is a wall-clock beat of the
        # recording rather than anything about the document.
        self.page = page
        # Locators resolve against the frame — the main window drives the site
        # iframe, while the overlay and the hold pause stay on the page.
        self.frame = frame
        self.approach = approach
        # "Is anything being filmed?" — the compile phase has no overlay, and
        # every branch below asks only that yes/no question of it, never the
        # overlay object itself.
        self.animated = animated
        # Pause (ms) after the option list unfurls, before the cursor sets off
        # towards the wanted row — the viewer needs a beat to read the list.
        self.open_hold_ms = open_hold_ms

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

        Two visible beats (spec §4): the cursor glides to the control and clicks
        it, the list unfurls, then the cursor glides to the wanted row and clicks
        that. Pages that enhance their own selects (select2, Tom Select) already
        have a DOM list; those are driven the same way, only the two click
        targets differ.

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
                that one control back to the browser (see
                :data:`~...select._js._PIN_NATIVE_JS`) — otherwise the cursor
                would be landing on a widget the hatch just opted out of.
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

        Without an overlay (``animated=False``, i.e. compile) the value is set
        directly and nothing is animated — compilation is meant to be fast, not
        pretty — but an enhanced widget is probed first
        (:func:`~...select.probe.probe_drivable`), so one with *nothing to click*
        surfaces here instead of after a multi-minute render. That probe is
        narrower than "drivable"; see its docstring for what it cannot see.

        Raises:
            SelectDriveError: nothing visible could be clicked for this select,
                the option is ``disabled``, or the option is not on offer —
                :attr:`~...select.errors.SelectDriveError.reason` tells the last
                of those apart from the rest on every path, direct ones included.
                The two listless paths ask
                :func:`~...select.probe.require_option` before they call
                ``select_option``, so neither refusal costs a step timeout and
                surfaces as a raw Playwright error.
            SelectsNotReadyError: the widget is installed in this frame but its
                first classification pass never finished (see
                :data:`~...select.probe.READY_WAIT_MS`).
        """

        # The readiness barrier: the classification pass decides whether this
        # select is shimmed, and every branch below asks that question.
        await probe.await_selects_ready(self.frame)
        if not self.animated:
            locator = await self._point_and_prepare(target)
            if native:
                await probe.pin_native(locator)
            else:
                await probe.probe_drivable(locator, option)
            await probe.require_option(locator, option)
            await self._reveal(on_revealed, SelectReveal(None, None))
            await probe.set_option_directly(locator, option)
            return
        if native:
            locator = await build_locator(self.frame, target)
            # Before the cursor sets off, not after: the shim's button and list
            # must be gone before this control is on camera, or the ripple would
            # land on a widget that vanishes out from under it.
            await probe.pin_native(locator)
            # Before the cursor sets off, too: neither an option this select does
            # not carry nor a `disabled` one can be chosen, and neither should
            # first cost a glide and a ripple towards a choice that cannot be
            # made.
            await probe.require_option(locator, option)
            box, center = await self.approach(locator, ripple=ripple, click_sound=True)
            # No row geometry: `native` never unfurls anything, so a still
            # capture can only show the collapsed control.
            await self._reveal(on_revealed, SelectReveal(box, center))
            await probe.set_option_directly(locator, option)
            return
        locator = await build_locator(self.frame, target)
        # Which of the three shapes this is, read from the DOM once and passed
        # down. "Not shimmed" is not on its own evidence of a page widget: the
        # shim also declines a natively-visible listbox, and that one has its
        # own single-beat path rather than an association heuristic to run.
        state = await probe.shim_state(locator)
        if state["listbox"]:
            await self._select_in_listbox(
                locator, state, option, ripple=ripple, on_revealed=on_revealed
            )
            return
        await self._select_in_two_beats(
            locator, state, option, ripple=ripple, on_revealed=on_revealed
        )

    async def diagnose(self, target: Target, option: str) -> SelectDriveError:
        """Why this ``<select>`` cannot be revealed, phrased for the author.

        The public face of :func:`~...select.errors.no_control_error`, for a
        caller that has already learned *that* a select is undrivable from
        somewhere else and now wants the situation named. The PDF guide's
        preflight is that caller: its reuse check answers ``not_visible``, which
        for a ``select`` action has exactly one cause —
        ``validate_compile_time``'s select arm reaches it only through
        ``user_visible_control() is None`` — but says so in a sentence shared
        with ``click``, ``hover`` and ``type``. Rather than write a second
        wording for the guide, it asks here and raises what the render would
        have raised.

        Returns the error rather than raising it so the caller can wrap it in
        its own step banner (`plik:linia` plus the YAML fragment) without having
        to catch what it just constructed.
        """

        locator = await build_locator(self.frame, target)
        state = await probe.shim_state(locator)
        return no_control_error(await probe.describe(locator), option, state)

    async def _point_and_prepare(self, target: Target) -> Locator:
        """Resolve the target and glide the cursor onto it, with the click beat.

        The recorder's own :meth:`...Recorder._point_and_prepare` spelled out
        against this object's two narrow dependencies, rather than borrowed from
        it: ``build_locator`` plus ``approach`` is the whole of it, and asking
        for a third constructor parameter to reach a two-line method would widen
        the seam for nothing.
        """

        locator = await build_locator(self.frame, target)
        await self.approach(locator, ripple=True, click_sound=True)
        return locator

    @staticmethod
    async def _reveal(hook: RevealHook | None, reveal: SelectReveal) -> None:
        """Hand the caller the open list's geometry, before anything is chosen."""

        if hook is not None:
            await hook(reveal)

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

        box, center = await self.approach(row, ripple=ripple, click_sound=True)
        await self._reveal(on_revealed, SelectReveal(control_box, control_center, box, center))
        await row.click()
        await probe.confirm_selected(select, option)

    async def _select_in_listbox(
        self,
        locator: Locator,
        state: dict,
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
        viewer can already see (``Recorder._approach`` does that scroll).

        The click's effect on *other* options is the same as the direct path's:
        an unmodified click replaces the whole selection, and so does
        ``select_option(label=…)``. A ``select:`` step has always meant "this one
        option is now chosen", and it still does.

        ``state`` is the caller's already-computed
        :func:`~...select.probe.shim_state` read — not re-fetched here, so this
        only ever needs the ``hidden`` half of it. A select the page (or an
        earlier step, or DOM drift since ``compile``) hid outright has no box for
        the cursor to land on, and neither ``locator.click()`` nor Playwright's
        own actionability wait for it would ever resolve; see
        :func:`~...select.errors.hidden_listbox_error` for why that failure has
        no ``mode: native`` escape hatch to offer.
        """

        if state["hidden"]:
            raise hidden_listbox_error(await probe.describe(locator), option)
        index = await probe.require_option(locator, option)
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
            control_center=center_of(control_box),
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
            raise no_control_error(await probe.describe(locator), option, state)
        else:
            control = await self._page_widget(locator, state, option)
        try:
            # Beat 2 of the page-widget path recognises the option rows by
            # "appeared after the click", so the snapshot has to be taken before
            # it.
            await self.frame.evaluate(_js._SNAPSHOT_JS)

            control_box, control_center = await self.approach(  # beat 1
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
            raise no_control_error(await probe.describe(locator), option, state)
        if not await control.is_visible():
            await control.dispose()
            raise no_control_error(await probe.describe(locator), option, state)
        return control

    async def _shim_option_row(self, locator: Locator, option: str) -> Locator:
        """Beat 2 for a shimmed select: the row the shim rendered, ready to click.

        Everything that has to be true *before* the cursor sets off happens here
        — the row exists, is visible, is not ``disabled``, and the list has been
        scrolled to it — and nothing that changes the page's state does. Handing
        the row back rather than clicking it is what lets the PDF guide take its
        frame in between; the click itself is :meth:`_commit_option`'s.

        The one path that does *not* ask :func:`~...select.probe.require_option`,
        and the reason is specific: by beat 2 the ``<select>`` is no longer the
        whole truth. The shim may have been taken off it while the list was
        opening, and what the cursor is about to land on is the shim's rendered
        row, so both questions have to be put to the rendered list. The
        *verdicts* are still the shared ones —
        :func:`~...select.errors.option_missing_error` and
        :func:`~...select.errors.disabled_option_error` — so a caller sees the
        same message and the same
        :attr:`~...select.errors.SelectDriveError.reason` here as anywhere else;
        only the thing being looked at differs.
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
            state = await probe.shim_state(locator)
            raise unshimmed_mid_step_error(
                await probe.describe(locator), option, state["markerClass"]
            )
        if index is None or index < 0:
            raise option_missing_error(await probe.describe(locator), option)
        # uid-scoped: the bare index attribute matches every shimmed select on the
        # page, which is a Playwright strict-mode violation.
        row = self.frame.locator(
            f'[data-guidebot-select-list][data-guidebot-for="{uid}"]'
            f' [data-guidebot-option-index="{index}"]'
        )
        try:
            await row.wait_for(state="visible", timeout=OPTION_WAIT_MS)
        except PlaywrightError as exc:
            raise no_option_error(
                await probe.describe(locator), option, limit_ms=OPTION_WAIT_MS
            ) from exc
        # Visible is not the same as clickable. A disabled row is rendered — at
        # `opacity: .45`, so a viewer can read it — and both `onListClick` and
        # `choose` return early for it. Clicking it would be a no-op the run has
        # no other way to notice, and sending the cursor there at all would film
        # a choice the page was never going to accept.
        if await row.get_attribute("data-guidebot-option-disabled") is not None:
            raise disabled_option_error(await probe.describe(locator), option)
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

        The underlying ``<select>`` is consulted before the wait, via the shared
        :func:`~...select.probe.require_option`, because it is the only thing
        here that can tell "the option is not on offer" from "the widget did not
        draw a row", and those two want different answers from a caller
        (:data:`~...select.errors.OPTION_MISSING`). A page widget keeps the
        original's ``<option>`` elements — that is what it submits — so the same
        question ``validate.reuse_failure`` asks at preflight is answerable here,
        ``disabled`` included. It also saves waiting :data:`OPTION_WAIT_MS` for a
        row that could not exist.
        """

        if not await self.frame.evaluate(_js._HAS_SNAPSHOT_JS):
            raise snapshot_lost_error(await probe.describe(locator), option)
        await probe.require_option(locator, option)
        try:
            handle = await self.frame.wait_for_function(
                _js._APPEARED_NODE_JS, arg=option, timeout=OPTION_WAIT_MS
            )
        except PlaywrightError as exc:
            raise no_option_error(
                await probe.describe(locator), option, limit_ms=OPTION_WAIT_MS
            ) from exc
        row = handle.as_element()
        if row is None:
            await handle.dispose()
            raise no_option_error(await probe.describe(locator), option, limit_ms=OPTION_WAIT_MS)
        # ``_commit_option``'s approach scrolls the row into view on both axes,
        # which is what scrolls an internally-scrolling widget list to it.
        return row
