"""``_render_step``: replay exactly one flat step.

**This is two dispatches on two different keys, and they are many-to-many.**

* :func:`_replay_scenario_kind` dispatches on the scenario ``kind`` —
  ``say``/``desktop``/``slide``/``closeWindow``/``navigate``/``wait``/``scroll``.
  Every one of those is complete in itself and there is no sidecar action to run.
* :func:`_replay_action` dispatches on the sidecar's ``cached.action`` —
  ``click``/``hover``/``type``/``select``/``highlight``/``waitFor`` — for the
  steps the first dispatch did *not* answer.

The relation between the two keys is not a function in either direction: a
``teach`` step freezes to any of the actions, and ``wait`` splits across both
depending on :meth:`Step.requires_target`. A single registry keyed on ``kind``
would therefore be structurally wrong — each handler would have to repeat the
frozen-action guards that sit *between* the dispatches. So the shape here is two
short ``if`` chains of one-line delegations, with :class:`_Replay` carrying the
context so a handler takes one argument instead of eight. ``say``, which needs
nothing at all, stays a two-line ``return`` rather than an empty implementation of
a uniform protocol.

**The second dispatch deliberately has no ``else``.** An unknown sidecar action
does nothing and raises nothing — today's behaviour, preserved verbatim through
this decomposition. It is a latent bug and it is tracked in the design's backlog;
fixing it is a behaviour change and belongs in its own commit with its own test.

``_render_step`` is a test seam: defined here, called through this module object
from :mod:`~guidebot_recorder.recorder.render.loop`.

``Overlay`` and ``Recorder`` are annotated through their module objects rather than
name-imported. Both are seams patched elsewhere — ``Recorder`` on
:mod:`~guidebot_recorder.recorder.render.visuals` and ``loop``, ``Overlay`` on
:mod:`~guidebot_recorder.recorder.render.stage` — and a bare name-import here
would be an import-time copy no patch reaches.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Frame, Page
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
from guidebot_recorder.overlay import overlay as overlay_module
from guidebot_recorder.recorder import recorder as recorder_module
from guidebot_recorder.recorder.recorder import SelectDriveError
from guidebot_recorder.resolver.resolution import ResolvedTarget, heuristic_expect
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError

from .errors import RenderError, _OptionalAbsent
from .narration import _narration
from .pages import navigate_pill_mode
from .popup_crop import _page_viewport, _start_popup_content_box
from .popup_detect import _popup_window_request
from .popup_session import _PageObservation, _PopupSession, _wait_for_render_popup
from .reuse import _freeze_resolved, _resolve_url
from .visuals import _ensure_visuals


@dataclass(frozen=True, slots=True)
class _Replay:
    """One step's replay context: everything the handlers below would otherwise take.

    Frozen, and built once at the top of :func:`_render_step` — including
    :attr:`pages_before`, which must be sampled *before* anything touches the
    page, or a popup that opened during the visual mount would look like one the
    click opened.
    """

    page: Page
    recorder: recorder_module.Recorder
    overlay: overlay_module.Overlay
    chrome: Chrome | None
    scenario: Scenario
    step: Step
    kind: str
    index: int
    anchor: float
    observed_pages: dict[Page, _PageObservation]
    ensure_card: Callable[[Page], Awaitable[None]]
    entry: FlatStep | None
    total: int
    sensitive: Iterable[str]
    expect_chrome: bool
    optional: bool
    scenario_hash: str
    pages_before: set[Page]
    action_frame: Page | Frame
    """Locators/navigation/reuse run against this: the site iframe for the main
    window (a Frame distinct from the shell page), the page itself for popups /
    chrome-disabled renders."""
    on_shell: bool

    def message(self, message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=self.index,
            total=self.total,
            location=self.entry.location if self.entry is not None else None,
            source=self.scenario.source,
            message=message,
            sensitive=self.sensitive,
        )


@dataclass(slots=True)
class _ClickWatch:
    """When the click really started, plus the guard that no popup preceded it.

    :meth:`before_click` is handed to ``Recorder.click`` as a **bound method**, so
    it reads the live page set at the instant the pointer goes down rather than a
    snapshot taken when this object was built.
    """

    ctx: _Replay
    started_at: float | None = None

    def before_click(self) -> None:
        if any(candidate not in self.ctx.pages_before for candidate in self.ctx.observed_pages):
            raise RenderError(self.ctx.message("popup otworzył się przed akcją click"))
        self.started_at = time.monotonic()


# --- dispatch A: the scenario `kind` ----------------------------------------- #


async def _replay_slide(ctx: _Replay) -> None:
    assert ctx.step.slide is not None  # guaranteed by command_kind()
    if _narration(ctx.step) is not None:
        # The loop already waited out the narration before calling us (one
        # picture timeline); re-assert the card and force a captured frame.
        await ctx.ensure_card(ctx.page)
        await ctx.page.screenshot()
        return
    # No `say` on this slide: hold the card ourselves, SPA-safe — re-assert
    # on a short cadence rather than a single blind sleep, so a same-
    # document rewrite mid-hold is repaired (and a real navigation still
    # fails loud via `ensure_card`'s token check).
    deadline = time.monotonic() + ctx.step.slide.hold
    while True:
        await ctx.ensure_card(ctx.page)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def _navigate_on_shell(ctx: _Replay, url: str, *, show_url: bool, mode: str) -> None:
    """Main window: the pill lives in the shell.

    The choreography/typed animation runs before goto; the truthful site URL
    (after redirects) is reflected once the iframe has loaded.
    """

    if show_url and mode in ("choreograph", "type"):
        await ctx.chrome.type_url(
            ctx.page,
            ctx.overlay,
            url,
            seed=f"{url}:{ctx.index}",
            choreograph=(mode == "choreograph"),
            on_sfx=ctx.recorder.on_sfx,
        )
    await ctx.recorder.navigate(url)
    if show_url:
        await ctx.chrome.set_url_shell(ctx.page, ctx.action_frame.url)


async def _navigate_in_page(ctx: _Replay, url: str, *, show_url: bool, mode: str) -> None:
    """Popup / chrome-disabled: legacy in-DOM pill on the page itself.

    An instant update happens after goto so redirects are reflected; the animated
    variant is typed before goto. A bare (floating) popup has no legacy bar/API
    (chrome.js bailed on barePopups), so gate the pill on ``expect_chrome`` —
    otherwise chrome.set_url would evaluate an undefined ``window.__guidebot_chrome``
    and throw an opaque TypeError.
    """

    if show_url and ctx.expect_chrome and mode != "instant":
        await ctx.chrome.set_url(ctx.page, url, animate=True)
    await ctx.recorder.navigate(url)
    if show_url and ctx.expect_chrome and mode == "instant":
        await ctx.chrome.set_url(ctx.page, ctx.page.url, animate=False)


async def _replay_navigate(ctx: _Replay) -> None:
    source_url = ctx.step.navigate_url()
    assert source_url is not None  # guaranteed by command_kind()
    url = _resolve_url(ctx.scenario, source_url)
    chrome_cfg = ctx.scenario.config.chrome
    show_url = ctx.chrome is not None and chrome_cfg.show_url
    mode = navigate_pill_mode(chrome_cfg, ctx.step.navigate_type_override())
    if ctx.on_shell:
        await _navigate_on_shell(ctx, url, show_url=show_url, mode=mode)
    else:
        await _navigate_in_page(ctx, url, show_url=show_url, mode=mode)
    await _ensure_visuals(ctx.page, ctx.overlay, ctx.chrome, expect_chrome=ctx.expect_chrome)


async def _replay_scenario_kind(ctx: _Replay) -> bool:
    """Steps the scenario ``kind`` answers on its own. True when the step is done.

    False means "this one is carried by the sidecar", and the second dispatch —
    plus the frozen-action guards between them — takes over.
    """

    if ctx.kind == "say":
        return True
    if ctx.kind == "desktop":
        # The opener's whole choreography (paint, cursor arc, double-click, window
        # growth) already ran in the card block before narration; nothing is left
        # to do in the action phase. Mirrors the visual-only `slide`/`say` returns.
        return True
    if ctx.kind == "slide":
        await _replay_slide(ctx)
        return True
    if ctx.kind == "closeWindow":
        # The loop's popup-lifecycle check sees the closed page next and runs
        # `visuals._prepare_main_after_popup_close` with the saved cursor position.
        # Do not duplicate that here: calling the funnel without
        # `restore_cursor_to` leaves the main window's cursor at the popup's centre.
        await ctx.page.close()
        return True
    if ctx.kind == "navigate":
        await _replay_navigate(ctx)
        return True
    if ctx.kind == "wait" and not ctx.step.requires_target():
        await ctx.recorder.wait_seconds(float(ctx.step.wait))
        return True
    if ctx.kind == "scroll":
        await ctx.recorder.scroll(ctx.step.scroll_config())
        return True
    return False


# --- between the dispatches: which frozen action is being replayed ------------ #


async def _identify_action(
    ctx: _Replay, cached: CompiledAction | None, resolved: ResolvedTarget | None
) -> CachedAction:
    if resolved is not None:
        # Resolved in place a moment ago against this very frame: it is live by
        # construction, and `expect` is only knowable after the action has run.
        return _freeze_resolved(ctx.step, ctx.kind, resolved, "none", ctx.scenario_hash)
    if cached is None:
        raise RenderError(ctx.message("brak cachedAction — uruchom `compile`"))
    if isinstance(cached, PendingAction):  # pragma: no cover - prologue rejects these
        raise RenderError(ctx.message("nierozwiązany wpis oczekujący — uruchom `compile`"))
    if cached.action != "waitFor" and not await reuse_is_valid(ctx.action_frame, cached):
        raise RenderError(ctx.message("niezgodna tożsamość — uruchom `compile --force`"))
    return cached


async def _frozen_action(
    ctx: _Replay, cached: CompiledAction | None, resolved: ResolvedTarget | None
) -> CachedAction:
    """The action to replay — freshly resolved or frozen — once it has been vetted."""

    action = await _identify_action(ctx, cached, resolved)
    if action.opens_popup and action.action != "click":
        raise RenderError(ctx.message("tylko click może otworzyć popup"))
    return action


# --- dispatch B: the sidecar `cached.action` ---------------------------------- #


async def _await_popup_page(ctx: _Replay, click_started_at: float) -> Page:
    """The one page the click opened, inside the actual-click discovery window."""

    popup_pages = await _wait_for_render_popup(
        ctx.observed_pages,
        ctx.pages_before,
        click_started_at,
    )
    if not popup_pages:
        raise RenderError(
            ctx.message("oczekiwany popup nie otworzył się — uruchom `compile --force`")
        )
    if len(popup_pages) != 1:
        raise RenderError("v1 obsługuje dokładnie jeden popup w sesji")
    popup_page = popup_pages[0]
    if await popup_page.opener() is not ctx.page:
        raise RenderError("nowa strona nie jest popupem aktywnego okna")
    return popup_page


async def _adopt_popup(ctx: _Replay, popup_page: Page) -> _PopupSession:
    """Turn the freshly opened page into the session record post-production reads."""

    observation = ctx.observed_pages.get(popup_page)
    if observation is None:  # defensive fallback; context event is the primary path
        observation = _PageObservation(
            opened_at=time.monotonic(),
            video=popup_page.video,
            closed_at=time.monotonic() if popup_page.is_closed() else None,
        )
        ctx.observed_pages[popup_page] = observation
    visual_ready_at = (
        await observation.visual_prime if observation.visual_prime is not None else None
    )
    popup_video = observation.video or popup_page.video
    if popup_video is None:  # pragma: no cover - context recording is enabled
        raise RenderError("Playwright nie udostępnił nagrania popupu")
    opened_at = max(0.0, observation.opened_at - ctx.anchor)
    closed_at = (
        max(opened_at, observation.closed_at - ctx.anchor)
        if observation.closed_at is not None
        else None
    )
    return _PopupSession(
        page=popup_page,
        video=popup_video,
        opened_at=opened_at,
        visual_ready_delay=(
            max(0.0, visual_ready_at - observation.opened_at)
            if visual_ready_at is not None
            else 0.0
        ),
        closed_at=closed_at,
        # Read from the opener while it is still alive.
        window_size=await _popup_window_request(ctx.page),
        # Available the instant the popup opens, and authoritative: a popup opened
        # with size features reports that size, a featureless one reports the
        # context viewport it inherited (which is exactly the case levels 2 and 3
        # exist for).
        viewport=_page_viewport(popup_page),
        # Level 2, started (not awaited) now because by composition time the popup
        # page is closed. Only the *painted* content can be measured: a featureless
        # popup's layout viewport is the context's, so innerWidth/outerWidth/
        # clientWidth all restate the oversized number instead of the real window.
        # Awaiting it here would spend its latency on camera; it is reaped before
        # the context closes instead.
        content_box_probe=_start_popup_content_box(popup_page),
    )


async def _replay_click(ctx: _Replay, cached: CachedAction) -> _PopupSession | None:
    watch = _ClickWatch(ctx)
    await ctx.recorder.click(cached.target, before_click=watch.before_click)
    if not cached.opens_popup:
        return None
    if watch.started_at is None:  # pragma: no cover - Recorder invariant
        raise RenderError("wewnętrzny błąd obserwacji akcji click")
    popup_page = await _await_popup_page(ctx, watch.started_at)
    return await _adopt_popup(ctx, popup_page)


async def _replay_type(ctx: _Replay, cached: CachedAction) -> None:
    input_text = ctx.step.enter_text.text if ctx.step.enter_text is not None else cached.input_text
    if input_text is None:
        raise RenderError(ctx.message("brak zamrożonego tekstu — uruchom `compile`"))
    await ctx.recorder.enter_text(cached.target, input_text)


async def _replay_select(ctx: _Replay, cached: CachedAction) -> None:
    if ctx.step.select is None:
        raise RenderError(ctx.message("brak opcji dla akcji select — uruchom `compile`"))
    try:
        await ctx.recorder.select(
            cached.target,
            ctx.step.select.option,
            native=select_mode(ctx.step, ctx.scenario.config) == "native",
        )
    except (SelectDriveError, SelectsNotReadyError) as exc:
        # No silent fallback to ``select_option``: that would restore exactly
        # the invisible magic this feature removes, and unobservably. The
        # banner is what makes the loud failure legible — it names the line
        # of the scenario the author has to edit, not just the widget.
        raise RenderError(ctx.message(str(exc))) from exc


async def _replay_highlight(ctx: _Replay, cached: CachedAction) -> None:
    if ctx.step.highlight is None:
        raise RenderError(
            ctx.message(
                "sidecar mówi `highlight`, a krok scenariusza nim nie jest "
                "— uruchom `compile --force`"
            )
        )
    await ctx.recorder.highlight(
        cached.target, ctx.step.highlight.resolved(ctx.scenario.config.highlight)
    )


async def _replay_wait_for(ctx: _Replay, cached: CachedAction) -> None:
    timeout = ctx.step.wait.timeout if isinstance(ctx.step.wait, WaitUntil) else 10.0
    try:
        await ctx.recorder.wait_for(cached.target, cached.state or "visible", timeout)
    except PlaywrightTimeoutError as exc:
        # The one absence signal a frozen gate can give: its wait window
        # elapsed. On a required step the timeout still fails the render.
        if not ctx.optional:
            raise
        raise _OptionalAbsent(f"upłynął czas oczekiwania ({timeout}s)") from exc


async def _replay_action(ctx: _Replay, cached: CachedAction) -> _PopupSession | None:
    """Replay the sidecar's frozen action. Returns the popup a click opened, if any.

    No ``else``: an unknown action does nothing and raises nothing. That is
    today's behaviour and it is preserved deliberately — see the module docstring.
    """

    if cached.action == "click":
        return await _replay_click(ctx, cached)
    if cached.action == "hover":
        await ctx.recorder.hover(cached.target)
    elif cached.action == "type":
        await _replay_type(ctx, cached)
    elif cached.action == "select":
        await _replay_select(ctx, cached)
    elif cached.action == "highlight":
        await _replay_highlight(ctx, cached)
    elif cached.action == "waitFor":
        await _replay_wait_for(ctx, cached)
    return None


# --- after the action --------------------------------------------------------- #


async def _finish_step(
    ctx: _Replay,
    cached: CachedAction,
    resolved: ResolvedTarget | None,
    url_before: str,
    on_resolved: Callable[[int, CachedAction], None] | None,
) -> None:
    """Derive ``expect``, hand a fresh resolution back, then wait for readiness."""

    expect = cached.expect
    if resolved is not None:
        # Mirror compile: the action reveals whether it navigated, and only then
        # is the entry complete enough to replace the pending one on disk.
        url_after = ctx.action_frame.url if not ctx.page.is_closed() else url_before
        expect = heuristic_expect(url_before, url_after)
        cached = _freeze_resolved(ctx.step, ctx.kind, resolved, expect, ctx.scenario_hash)
        if on_resolved is not None:
            on_resolved(ctx.index, cached)

    if not ctx.page.is_closed():
        try:
            await ctx.recorder.apply_readiness(expect)
        except PlaywrightError:
            if not ctx.page.is_closed():
                raise


async def _render_step(
    page: Page,
    recorder: recorder_module.Recorder,
    overlay: overlay_module.Overlay,
    chrome: Chrome | None,
    scenario: Scenario,
    step: Step,
    kind: str,
    index: int,
    cached: CompiledAction | None,
    anchor: float,
    observed_pages: dict[Page, _PageObservation],
    ensure_card: Callable[[Page], Awaitable[None]],
    *,
    entry: FlatStep | None = None,
    total: int = 0,
    sensitive: Iterable[str] = (),
    expect_chrome: bool | None = None,
    resolved: ResolvedTarget | None = None,
    optional: bool = False,
    scenario_hash: str = "",
    on_resolved: Callable[[int, CachedAction], None] | None = None,
) -> _PopupSession | None:
    """Replay one flat step.

    ``resolved`` carries a target the caller resolved in place for a
    :class:`PendingAction`; this call performs it, derives its ``expect`` the way
    ``compile`` does (URL before vs after) and hands the frozen action back through
    ``on_resolved`` so the sidecar stops being pending.

    ``entry`` (plus ``total`` i ``sensitive``) służy wyłącznie diagnostyce:
    komunikaty błędów wskazują `plik:linia` i cytują fragment YAML-a. Wszystkie
    trzy są keyword-only i mają wartości domyślne — pozycje argumentów pozostają
    nietknięte, a bez nich banner degraduje się do samego numeru kroku.
    """

    action_frame = getattr(recorder, "frame", recorder.page)
    ctx = _Replay(
        page=page,
        recorder=recorder,
        overlay=overlay,
        chrome=chrome,
        scenario=scenario,
        step=step,
        kind=kind,
        index=index,
        anchor=anchor,
        observed_pages=observed_pages,
        ensure_card=ensure_card,
        entry=entry,
        total=total,
        sensitive=sensitive,
        expect_chrome=(chrome is not None) if expect_chrome is None else expect_chrome,
        optional=optional,
        scenario_hash=scenario_hash,
        pages_before=set(observed_pages),
        action_frame=action_frame,
        on_shell=action_frame is not recorder.page,
    )
    # Both visual layers can be removed by an SPA without a navigation.  Check
    # them before every recorded step, including narration-only and timed waits.
    # ``expect_chrome`` is False when ``page`` is a bare (floating) popup.
    await _ensure_visuals(page, overlay, chrome, expect_chrome=ctx.expect_chrome)

    if await _replay_scenario_kind(ctx):
        return None

    url_before = ctx.action_frame.url
    frozen = await _frozen_action(ctx, cached, resolved)
    opened = await _replay_action(ctx, frozen)
    await _finish_step(ctx, frozen, resolved, url_before, on_resolved)
    return opened
