"""Replaying one compiled step in the browser: the state, and the phases.

:mod:`~guidebot_recorder.guide.capture` owns the loop; this module owns
everything one turn of it needs — :class:`_Capture` (what outlives a step),
:class:`_StepRun` (the step and the frame it contributes), and every phase that
does **not** consume a test seam.

That last clause is the whole reason for the file boundary, and it runs the
other way from the usual one: ``reuse_failure``, ``annotations_for`` and
``pause_for_inspection`` are patched on the ``capture`` module, so their
consumers have to stay in ``capture`` — a copy of one of those names in *this*
module's globals would be a copy no patch reaches. Everything else is free to
live here, and does. ``tests/unit/guide/test_capture_seams.py`` turns that
sentence into an assertion.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.guide.stills import _OpenListFrame, _screenshot
from guidebot_recorder.guide.trail import _CursorTrail
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import FlatStep, Step, WaitUntil, select_mode
from guidebot_recorder.recorder.recorder import (
    OPTION_MISSING,
    Recorder,
    SelectDriveError,
)
from guidebot_recorder.selects import Selects, SelectsNotReadyError

_Point = tuple[float, float]


def scenario_resolve_url(scenario, url: str | None) -> str:
    """Resolve a possibly-relative navigate URL against the scenario base_url.

    Mirrors ``_resolve_url`` in ``recorder/render/reuse.py``: relative URLs are
    joined onto ``config.base_url`` with ``urljoin`` only when a base is
    configured; an absolute URL, or the absence of a base, passes through
    unchanged. ``url`` is defensively allowed to be ``None`` (navigate steps
    always carry a URL in practice, but the type isn't statically guaranteed).
    """
    base = scenario.config.base_url
    if url is None:
        return base or ""
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


@dataclass
class _Capture:
    """What outlives one step of the capture pass.

    :attr:`pages` is the document being built, :attr:`skipped_branch` the gate
    that turned out to be absent, and :attr:`trail` the cursor memory.

    :attr:`page` and :attr:`recorder` are the **active window**, not the main one.
    A step that opens a popup swaps both (:meth:`enter_popup`) and ``closeWindow``
    swaps them back (:meth:`leave_popup`); every ``_screenshot(cap.page, ...)`` in
    this module photographs whichever window is active at the time, which is the
    whole of the popup support. The main pair is kept aside in ``__post_init__``
    so leaving never has to reconstruct it.
    """

    scenario: object
    page: Page
    recorder: Recorder
    shots_dir: Path
    flat: list[FlatStep]
    timeout: float
    verbose: bool
    pause_on_error: bool
    sensitive_values: Iterable[str]
    selects: Selects | None
    #: Builds the popup's own ``Recorder``. Injected rather than constructed here
    #: because a ``Recorder`` needs the ``Overlay``, which belongs to the caller's
    #: browser context — and because a test can hand over a fake without a browser.
    #: ``None`` in a capture that never meets a popup.
    make_recorder: Callable[[Page], Recorder] | None = None
    pages: list[GuidePage] = field(default_factory=list)
    trail: _CursorTrail = field(default_factory=_CursorTrail)
    skipped_branch: int | None = None
    popup: Page | None = None

    def __post_init__(self) -> None:
        self._main_page = self.page
        self._main_recorder = self.recorder

    @property
    def canvas(self) -> tuple[int, int]:
        """The size every page is presented at — the main window's viewport.

        A popup still is smaller than this and gets centred on it by the layout;
        see :func:`~guidebot_recorder.guide.layout._letterbox`.
        """

        viewport = self.scenario.config.viewport
        return (viewport.width, viewport.height)

    def enter_popup(self, popup: Page) -> None:
        """Make ``popup`` the active window for the steps that follow."""

        if self.make_recorder is None:
            raise RuntimeError(
                "guide nie ma fabryki Recordera, a krok otworzył popup — "
                "to błąd wywołania `capture_pages`, nie scenariusza"
            )
        self.popup = popup
        self.page = popup
        self.recorder = self.make_recorder(popup)
        # A new window is a new coordinate system: an arrow drawn from the last
        # target in the other window would point at nothing on this picture.
        self.trail.reset()

    async def leave_popup(self) -> None:
        """Close the popup, if one is open, and go back to the main window.

        A no-op when no popup is open: `closeWindow` in a scenario that never
        opened one is bookkeeping the author wrote for the film, and the guide has
        nothing to undo.
        """

        if self.popup is None:
            return
        if not self.popup.is_closed():
            await self.popup.close()
        self.popup = None
        self.page = self._main_page
        self.recorder = self._main_recorder
        self.trail.reset()

    def banner(self, entry: FlatStep, entry_index: int, message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=entry_index,
            total=len(self.flat),
            location=entry.location,
            source=self.scenario.source,
            message=message,
            sensitive=self.sensitive_values,
        )

    def skipping(self, entry: FlatStep) -> bool:
        """Whether this step belongs to a branch whose gate was absent."""

        if self.skipped_branch is None:
            return False
        if entry.branch == self.skipped_branch:
            return True
        self.skipped_branch = None
        return False

    def skip_branch(self, run: _StepRun) -> None:
        """Drop the rest of the branch: its gate element is not on the page."""

        self.skipped_branch = run.entry.branch
        if self.verbose:
            tqdm.write(f"pomijam gałąź {run.entry.branch}: bramka nieobecna")

    def note(self, run: _StepRun, message: str) -> None:
        """Say why a step produced no page — only when the run is verbose."""

        if self.verbose:
            tqdm.write(run.banner(message))

    async def await_selects_ready(self, run: _StepRun) -> None:
        """Take the shim's readiness barrier, blaming the step that was running.

        Taken twice: after every navigation, because the first frame of a new
        document is photographed immediately and must show the same DOM compile
        resolved against; and again before a ``select:`` step drives its control.
        The second is not covered by the first — ``wait_ready`` answers "is a
        pass owed *right now*", so a navigation-time answer says nothing about a
        select the page grew three steps later, and a select the pending pass has
        not reached yet is a bare ``<select>`` with no DOM list to unfurl.

        ``Recorder.select`` takes a barrier of its own, but that one is
        explicitly the *backstop* for a direct caller: it reads the page API
        itself on a flat, generous bound, knowing nothing of ``settle_ms``. The
        guide is a production caller like compile and render, so like them it
        takes the controller's bounded barrier first — which is also what turns
        a wedged widget into this step's banner (`plik:linia` plus the YAML
        fragment) instead of a bare exception from inside the recorder.
        """

        if self.selects is None:
            return
        try:
            await self.selects.wait_ready(self.recorder.frame)
        except SelectsNotReadyError as exc:
            raise GuideError(run.banner(str(exc))) from exc


@dataclass
class _StepRun:
    """One step of the capture pass, and the frame it contributes to the guide.

    The fields below :attr:`kind` are filled in by the phases: the approach
    records where the target is, the frame handler records the still it took and
    — for a ``select:`` whose list was photographed open — the option row that
    supersedes the control as the cursor's resting place.
    """

    capture: _Capture
    index: int
    entry: FlatStep
    action: CompiledAction | None
    kind: str

    locator: object = None
    shot: Path | None = None
    size: tuple[int, int] | None = None
    center: _Point | None = None
    box: dict | None = None
    row_box: dict | None = None
    row_center: _Point | None = None
    mark: object = None
    #: The window this step's click opened, filled by :func:`_click_or_hover_frame`
    #: for a frozen ``opens_popup`` click. The switch itself happens one phase
    #: later, in ``capture._action_page``: this step's own page still belongs to
    #: the window the reader was looking at when they clicked.
    popup: Page | None = None

    @property
    def step(self) -> Step:
        return self.entry.step

    @property
    def act(self) -> str:
        """The frozen action this step replays. Only meaningful on action pages."""

        return self.action.action

    def banner(self, message: str) -> str:
        """This step's message, with `plik:linia` and the YAML fragment."""

        return self.capture.banner(self.entry, self.index, message)


async def _gate_page(run: _StepRun) -> None:
    """Wait for the branch's gate element; an absent gate skips the whole branch."""

    cap = run.capture
    try:
        target = run.action.target if isinstance(run.action, CachedAction) else None
        if target is None:
            cap.skip_branch(run)
            return
        # action is a CachedAction here (target came from it above); mirror the
        # non-gate wait branch's state fallback and use the gate's own timeout.
        state = run.action.state or "visible"
        wait = run.step.wait
        gate_timeout = wait.timeout if isinstance(wait, WaitUntil) else cap.timeout
        await cap.recorder.wait_for(target, state, gate_timeout)
    except PlaywrightError:
        cap.skip_branch(run)  # branch element absent -> skip whole branch


async def _navigate_page(run: _StepRun) -> None:
    """Load the URL, photograph the new document, and start the trail over."""

    cap = run.capture
    url = scenario_resolve_url(cap.scenario, run.step.navigate_url())
    await cap.recorder.navigate(url)
    await cap.await_selects_ready(run)
    shot, size = await _screenshot(cap.page, cap.shots_dir, run.index)
    cap.pages.append(
        GuidePage(
            kind="navigate",
            screenshot=shot,
            text=page_text(run.step),
            heading=f"Otwórz adres: {url}",
            annotations=[],
            screenshot_size=size,
            canvas_size=cap.canvas,
        )
    )
    cap.trail.reset()


async def _slide_page(run: _StepRun) -> None:
    """A full-page interstitial: no browser work, no screenshot."""

    slide = run.step.slide
    run.capture.pages.append(
        GuidePage(
            kind="slide",
            screenshot=None,
            text=slide.subtitle or slide.notes or "",
            heading=slide.title,
            annotations=[],
        )
    )


async def _text_page(run: _StepRun) -> None:
    """Narration with nothing to photograph."""

    run.capture.pages.append(
        GuidePage(
            kind="text",
            screenshot=None,
            text=page_text(run.step),
            heading=None,
            annotations=[],
        )
    )


async def _scroll_page(run: _StepRun) -> None:
    """Scroll the page; produce a page of its own only when the step narrates."""

    cap = run.capture
    await cap.recorder.scroll(run.step.scroll_config())
    cap.trail.reset()
    text = page_text(run.step)
    if not text:
        return
    shot, size = await _screenshot(cap.page, cap.shots_dir, run.index)
    cap.pages.append(
        GuidePage(
            kind="step",
            screenshot=shot,
            text=text,
            heading=None,
            annotations=[],
            screenshot_size=size,
            canvas_size=cap.canvas,
        )
    )


async def _wait_page(run: _StepRun) -> None:
    """Wait — either a bare number of seconds or a frozen ``waitFor``."""

    cap = run.capture
    step = run.step
    if isinstance(step.wait, int | float):
        await cap.recorder.wait_seconds(float(step.wait))
        return
    if isinstance(run.action, CachedAction) and run.action.action == "waitFor":
        timeout_wait = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        await cap.recorder.wait_for(run.action.target, run.action.state or "visible", timeout_wait)
    elif cap.verbose and not step.optional:
        tqdm.write(run.banner("pomijam: oczekiwanie nierozwiązane — uruchom `compile`"))


async def _approach_target(run: _StepRun) -> bool:
    """Point the cursor at the target, reporting whether the step can go on.

    Doubles as the "is the target here at all?" probe, which is why `select`
    goes through it too even though its choreography approaches the control
    again: only a *resolution* failure here means an optional step's target is
    absent, and widening the `except` to cover the choreography would start
    reading a failed click as one.
    """

    cap = run.capture
    try:
        res = await cap.recorder.point(run.action.target, ripple=False)
    except PlaywrightError:
        if run.step.optional:
            cap.note(run, "pomijam: cel nieobecny")
            return False
        raise
    # Where this step's marks go. For every action but `select` that is the
    # control the cursor just approached; `select` re-derives it from the open
    # list, because by the time its frame is taken `res.box` is the *collapsed*
    # control's and stale.
    run.locator, run.center, run.box = res.locator, res.center, res.box
    return True


async def _type_frame(run: _StepRun) -> bool:
    cap, step = run.capture, run.step
    text = (step.enter_text.text if step.enter_text else None) or run.action.input_text
    if text is None:
        raise GuideError(run.banner("brak zamrożonego tekstu — uruchom `compile`"))
    await run.locator.fill(text)
    run.shot, run.size = await _screenshot(cap.page, cap.shots_dir, run.index)  # frame AFTER typing
    return True


async def _select_frame(run: _StepRun) -> bool:
    cap, step = run.capture, run.step
    if step.select is None:
        # Plain `compile`, not `--force`: editing the step changed its
        # command_kind, so the fingerprint already fails to match and the
        # entry is re-resolved on its own. `--force` would pointlessly
        # re-freeze every other step in the scenario.
        raise GuideError(
            run.banner("sidecar mówi `select`, a krok scenariusza nim nie jest — uruchom `compile`")
        )
    # The step that actually drives a `<select>`, so the barrier is taken again
    # here — see `_Capture.await_selects_ready`.
    await cap.await_selects_ready(run)
    # Named `still`, not `frame`: in this codebase `frame` means a
    # Playwright frame everywhere else.
    still = _OpenListFrame(cap.page, cap.shots_dir, run.index)
    try:
        # `ripple=False` for the same reason `_approach_target` uses it: a still
        # capture wants a clean frame, not a click ring frozen mid-animation.
        await cap.recorder.select(
            run.action.target,
            step.select.option,
            native=select_mode(step, cap.scenario.config) == "native",
            ripple=False,
            on_revealed=still,
        )
    except SelectDriveError as exc:
        # `optional` has to cover choosing the option, not just finding the
        # control: an optional step skips reuse validation, so a vanished option
        # shows up here — and only here. Narrowed to that one reason on purpose:
        # every other `SelectDriveError` (a click that did not take, a widget
        # with nothing to unfurl, a shim removed mid-step) says the step *is*
        # broken, and skipping those would hide exactly the failures this
        # choreography was written to surface.
        if step.optional and exc.reason == OPTION_MISSING:
            cap.note(run, "pomijam: brak żądanej opcji")
            return False
        # No silent fallback to `select_option`: it would restore exactly the
        # invisible value change this capture exists to remove, and the PDF would
        # look fine while being useless.
        raise GuideError(run.banner(str(exc))) from exc
    except SelectsNotReadyError as exc:
        raise GuideError(run.banner(str(exc))) from exc
    if still.shot is None or still.size is None or still.reveal is None:
        raise GuideError(run.banner("krok `select` nie oddał kadru z rozwiniętą listą"))
    run.shot, run.size = still.shot, still.size
    run.center, run.box = still.reveal.control_center, still.reveal.control_box
    run.row_box, run.row_center = still.reveal.row_box, still.reveal.row_center
    return True


async def _highlight_frame(run: _StepRun) -> bool:
    cap, step = run.capture, run.step
    if step.highlight is None:
        raise GuideError(
            run.banner(
                "sidecar mówi `highlight`, a krok scenariusza nim nie jest "
                "— uruchom `compile --force`"
            )
        )
    # Deliberately no action on the element: `highlight` never touches the page,
    # and `_click_or_hover_frame` would click it. The mark itself is drawn onto
    # the page by the annotation, not by the browser.
    run.mark = step.highlight.resolved(cap.scenario.config.highlight)
    run.shot, run.size = await _screenshot(cap.page, cap.shots_dir, run.index)
    return True


def _opens_popup(action: CompiledAction | None) -> bool:
    """Whether ``compile`` froze this action as the one that opens a new window."""

    return isinstance(action, CachedAction) and action.opens_popup


async def _click_into_popup(run: _StepRun) -> Page:
    """Click, and hand back the window that click opened.

    The wait wraps the click rather than following it: a popup that opens fast
    would have fired its event before a bare ``wait_for_event`` started listening,
    and the guide would then blame the site for its own race.
    """

    cap = run.capture
    try:
        async with cap.page.context.expect_page(timeout=cap.timeout * 1000) as opened:
            await run.locator.click()
        popup = await opened.value
    except PlaywrightTimeoutError as exc:
        # The frozen flag says a window opens here, so its absence is a broken
        # guide, not a branch to skip: a PDF missing the popup's steps would read
        # as a complete set of instructions while omitting half of them.
        raise GuideError(
            run.banner("krok miał otworzyć nowe okno, a żadne się nie pojawiło")
        ) from exc
    await popup.wait_for_load_state("domcontentloaded")
    return popup


async def _click_or_hover_frame(run: _StepRun) -> bool:
    cap = run.capture
    # frame BEFORE click/hover
    run.shot, run.size = await _screenshot(cap.page, cap.shots_dir, run.index)
    if run.act == "hover":
        await run.locator.hover()
    elif _opens_popup(run.action):
        run.popup = await _click_into_popup(run)
    else:
        await run.locator.click()
    return True


async def _close_window_page(run: _StepRun) -> None:
    """Close the popup, go back to the main window, and narrate if the step does.

    No screenshot of its own: the closing is not a thing the reader does, it is
    the film's way of getting back — and the next step photographs the main window
    anyway.
    """

    cap = run.capture
    await cap.leave_popup()
    text = page_text(run.step)
    if not text:
        return
    cap.pages.append(
        GuidePage(kind="text", screenshot=None, text=text, heading=None, annotations=[])
    )


#: How each frozen action gets photographed. `click` and `hover` share the
#: default because they differ only in the call that follows the frame.
_FRAMES: dict[str, Callable[[_StepRun], Awaitable[bool]]] = {
    "type": _type_frame,
    "select": _select_frame,
    "highlight": _highlight_frame,
}
