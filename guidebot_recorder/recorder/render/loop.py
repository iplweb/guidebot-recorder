"""The render loop: one flat step at a time, against the plan, the stage and the clock.

This is where the three lifetimes meet, so nothing new is stored here — every
phase below reads :class:`~guidebot_recorder.recorder.render.plan._RenderPlan`
(what was decided), mutates
:class:`~guidebot_recorder.recorder.render.stage._Stage` (what is on screen) and
:class:`~guidebot_recorder.recorder.render.clock._Clock` (where things land on the
recording axis). :class:`_StepCtx` bundles the three plus the step under work, so
the phases take one argument instead of nine.

**The absence probe runs before the narration, and that is now a seam rather than
a comment.** An optional step that turns out to be absent must not narrate first
and only then do nothing, so :func:`_probe_absence` — "does this step happen at
all" — is a separate function from :func:`_narrate` and :func:`_perform` — "make
it happen". Everything decidable without acting (a stale frozen target, an
unresolvable pending entry) is decided in the first; a cached gate is the one
exception, because its ``waitFor`` *is* the action, so it stays inside
``_step._render_step`` and comes back out as ``_OptionalAbsent``. Both absences
land in :func:`_note_absent`.

``Recorder`` is a test seam: name-imported here because this module constructs
one, so a patch on *this* module is what has to reach it. The same class is also
constructed in :mod:`~guidebot_recorder.recorder.render.visuals`, so replacing it
takes **two** patch lines. ``_render_step`` and ``_prepare_main_after_popup_close``
are seams called through their module objects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import NoReturn

from playwright.async_api import Frame, Page
from tqdm import tqdm

from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import FlatStep, Step
from guidebot_recorder.recorder._debug import pause_for_inspection, redact_exception
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import ResolvedTarget
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError

from . import _step, visuals
from .clock import _Clock
from .errors import RenderError, _OptionalAbsent
from .plan import _RenderPlan
from .popup_detect import _popup_window_opened
from .popup_session import _PopupSession, _prepare_popup
from .reuse import _resolve_pending_target
from .stage import _close_stage, _Stage
from .visuals import _hand_cursor_to_popup, _play_desktop_opener

#: Force a bounded final frame after narration/action completion. Without this
#: post-roll, a static last page can leave the VFR recording a fraction shorter
#: than the audio timeline and make the final syllable trimmable.
_VIDEO_POSTROLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class _LoopOptions:
    """The caller's knobs, unchanged for the whole run."""

    timeout: float
    pause_on_error: bool
    verbose: bool
    reasoner: Reasoner | None


@dataclass(frozen=True, slots=True)
class _StepCtx:
    """One flat step and the three objects every phase of it needs."""

    plan: _RenderPlan
    stage: _Stage
    clock: _Clock
    opts: _LoopOptions
    entry: FlatStep
    index: int
    step: Step
    kind: str
    cached: CompiledAction | None
    optional: bool
    """Whether absence is tolerated here.

    The only two places it is: a branch gate and an ``optional: true`` step. A
    *child* of an entered branch is NOT optional — the branch demonstrably
    happened, so anything failing inside it is a real regression (§5 of the
    design). Its pending entry is still resolved; only the verdict on absence
    differs.
    """

    @classmethod
    def of(
        cls,
        plan: _RenderPlan,
        stage: _Stage,
        clock: _Clock,
        opts: _LoopOptions,
        entry: FlatStep,
        index: int,
    ) -> _StepCtx:
        return cls(
            plan=plan,
            stage=stage,
            clock=clock,
            opts=opts,
            entry=entry,
            index=index,
            step=entry.step,
            kind=entry.step.command_kind(),
            cached=plan.compiled.actions[index],
            optional=entry.is_gate or entry.step.optional,
        )

    def message(self, text: str) -> str:
        return self.plan.step_message(self.entry, self.index, text)


def _note_absent(ctx: _StepCtx, absent: _OptionalAbsent) -> int | None:
    """Record a tolerated absence; return the branch whose children to skip.

    Both absence signals end here — the one the probe raises before the step
    narrates, and the one a cached gate's timed-out ``waitFor`` raises from inside
    ``_render_step``.
    """

    ctx.plan.note_skip(ctx.entry, ctx.index, str(absent), gate=ctx.entry.is_gate)
    return ctx.entry.branch if ctx.entry.is_gate else None


async def _fail_step(ctx: _StepCtx, exc: BaseException) -> NoReturn:
    """The one funnel for "this step failed": redact, report, optionally pause.

    Everything the resolver rejects for a reason other than absence —
    ``multiple_actions`` above all — is an authoring bug and fails the render,
    exactly like a failure of the action itself.
    """

    safe_message = redact_exception(exc, ctx.plan.sensitive_values)
    if ctx.opts.verbose:
        tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
    if ctx.opts.pause_on_error:
        await pause_for_inspection(
            ctx.stage.active_page,
            "render",
            ctx.index,
            ctx.kind,
            exc,
            ctx.plan.sensitive_values,
            total=ctx.plan.total,
            location=ctx.entry.location,
            source=ctx.plan.scenario.source,
        )
    raise RenderError(f"{type(exc).__name__}: {safe_message}") from None


def _assert_pages_intact(ctx: _StepCtx, closed_message: str) -> None:
    """The deterministic page contract, re-checked before the step begins."""

    ctx.stage.sync_popup_close()
    if ctx.stage.popup_closed_unhandled():
        raise RenderError(closed_message)
    if ctx.stage.unexpected_pages():
        raise RenderError(ctx.message("nieoczekiwany popup — uruchom `compile --force`"))


async def _prepare_visuals(ctx: _StepCtx) -> Page:
    """Card-aware visual prep, ahead of the narration block.

    A ``slide`` step paints (replacing any prior card); a ``say`` step keeps a
    live card up while it narrates; any other step dismisses the card first
    (asserting it survived, fail-loud) before its normal ``_ensure_visuals``. With
    no card ever painted this is exactly today's unconditional ``_ensure_visuals``
    call (back-compat).
    """

    stage = ctx.stage
    step = ctx.step
    active_page = stage.active_page
    await active_page.bring_to_front()
    if ctx.kind == "desktop":
        assert step.desktop is not None  # guaranteed by command_kind()
        if stage.card is not None:
            await stage.hide_card(active_page)
        await _play_desktop_opener(
            stage.desktop,
            stage.overlay,
            active_page,
            ctx.plan.desktop_payloads[ctx.index],
            hold=step.desktop.hold,
            settle_ms=ctx.plan.cfg.cursor.settle,
            reveal=partial(stage.chrome_show, active_page),
            on_click=(ctx.clock.note_sfx if ctx.plan.cfg.sound.enabled else None),
        )
        # The opener ends on the revealed chrome shell — normal visible state, so
        # from here it is exactly the no-card path (`stage.card` stays None).
    elif ctx.kind == "slide":
        assert step.slide is not None  # guaranteed by command_kind()
        if stage.card is not None:
            # Fail loud before repainting: a slide following a say whose card was
            # destroyed mid-narration must NOT silently swap in a fresh card over
            # the wrong page (`reveal_page` asserts the token, exactly like the
            # generic dismiss branch below).
            await stage.reveal_page(active_page)
        await stage.show_card(
            active_page,
            {
                "title": step.slide.title,
                "subtitle": step.slide.subtitle,
                "notes": step.slide.notes,
            },
        )
    elif ctx.kind == "say" and stage.card is not None:
        await stage.ensure_card(active_page)
    elif stage.card is not None:
        await stage.reveal_page(active_page)
        await stage.ensure_visuals(active_page, expect_chrome=stage.expect_chrome)
    else:
        await stage.ensure_visuals(active_page, expect_chrome=stage.expect_chrome)
    return active_page


async def _wait_selects_ready(ctx: _StepCtx, probe_root: Page | Frame) -> None:
    """Readiness barrier for the DOM select shim, the mirror of compile's.

    Both the in-place resolution below and the frozen-target check inside
    ``_render_step`` must see the shimmed DOM, or render would drive a page
    compile never resolved against. Any navigation that led here has settled — it
    was an earlier step.
    """

    if ctx.stage.selects is None or not ctx.step.requires_target():
        return
    try:
        await ctx.stage.selects.wait_ready(probe_root)
    except SelectsNotReadyError as exc:
        # The barrier sits outside every per-step ``except`` in the loop, so
        # without this the one failure that stops a render before its step even
        # begins would be the only one to reach the author with no file, no line
        # and no YAML fragment.
        raise RenderError(ctx.message(str(exc))) from exc


async def _resolve_in_place(ctx: _StepCtx, probe_root: Page | Frame) -> ResolvedTarget | None:
    """Resolve a pending entry, or re-verify a frozen one. Raises on absence."""

    cached = ctx.cached
    if isinstance(cached, PendingAction):
        if ctx.opts.reasoner is None:
            raise _OptionalAbsent(
                "brak dostępnego reasonera, a krok nie został skompilowany "
                "(pending) — zainstaluj `codex`, aby rozwiązać go na miejscu"
            )
        return await _resolve_pending_target(probe_root, ctx.step, ctx.kind, ctx.opts.reasoner)
    if isinstance(cached, CachedAction) and cached.action != "waitFor":
        if not await reuse_is_valid(probe_root, cached):
            raise _OptionalAbsent("zamrożony namiar nie pasuje do strony")
    return None


async def _probe_absence(ctx: _StepCtx, active_page: Page) -> ResolvedTarget | None:
    """Does this step happen at all? Decided **before** a single word is narrated.

    Raises :class:`_OptionalAbsent` when the answer is "no" and that is tolerated,
    :class:`RenderError` when it is not. A cached gate is the one case that cannot
    be answered here — its ``waitFor`` IS the action — so it stays in
    ``_step._render_step``; a synthetic gate step never narrates anyway.
    """

    stage = ctx.stage
    # The site iframe for the main window, the page itself for popups /
    # chrome-disabled renders — never the shell document, which the shim
    # deliberately skips.
    probe_root: Page | Frame = (
        stage.site_frame
        if active_page is stage.page and stage.site_frame is not None
        else active_page
    )
    await _wait_selects_ready(ctx, probe_root)
    if not (ctx.step.requires_target() and (ctx.optional or isinstance(ctx.cached, PendingAction))):
        return None
    try:
        return await _resolve_in_place(ctx, probe_root)
    except _OptionalAbsent as absent:
        if not ctx.optional:
            raise RenderError(ctx.message(str(absent))) from None
        raise
    except Exception as exc:
        await _fail_step(ctx, exc)


async def _narrate(ctx: _StepCtx) -> Page:
    """Spend the step's voice-over, then re-assert the picture it played over.

    A navigation that destroyed the card DURING the narration wait (a say/slide
    over a live card) must fail loud here — this is the checkpoint that catches a
    mid-wait destruction even when the say is the LAST step (the loop still fully
    processes that step before exiting). When no card is active this is exactly
    today's unconditional ``_ensure_visuals`` (back-compat).
    """

    stage = ctx.stage
    # Recording-axis frame: the mapping onto the finished film needs the complete
    # edit list, which does not exist until the loop ends.
    step_segments, narration_frame = ctx.clock.place_narration(
        ctx.index, ctx.plan.audio_configs, ctx.plan.segments
    )
    if step_segments:
        await ctx.clock.pace(step_segments, ctx.plan.cfg, not_before=narration_frame)

    stage.sync_popup_close()
    if stage.popup_closed_unhandled():
        raise RenderError("popup zamknął się asynchronicznie podczas narracji")
    active_page = stage.active_page
    if stage.unexpected_pages():
        raise RenderError(ctx.message("nieoczekiwany popup — uruchom `compile --force`"))
    await active_page.bring_to_front()
    if stage.card is not None:
        await stage.ensure_card(active_page)
    else:
        await stage.ensure_visuals(active_page, expect_chrome=stage.expects_bar(active_page))
    return active_page


def _build_recorder(ctx: _StepCtx, active_page: Page) -> Recorder:
    """The driver for this step's action, wired to this step's page."""

    cfg = ctx.plan.cfg
    stage = ctx.stage
    # Main window drives the site iframe (a Frame); popups drive the page.
    on_shell = active_page is stage.page and stage.site_frame is not None
    return Recorder(
        active_page,
        stage.overlay,
        settle_ms=cfg.cursor.settle,
        frame=stage.site_frame if on_shell else None,
        type_delay_ms=(cfg.typing.speed if cfg.typing.animate else None),
        type_jitter_ms=cfg.typing.jitter_ms,
        type_max_delay_factor=cfg.typing.max_delay_factor,
        on_sfx=(ctx.clock.note_sfx if cfg.sound.enabled else None),
        # How long the unfurled option list is held before the cursor sets off
        # towards the chosen row. Render is the only phase that animates a
        # `select:` step, so this is the one place the configured value can take
        # effect at all.
        open_hold_ms=cfg.selects.open_hold_ms,
    )


async def _perform(
    ctx: _StepCtx, active_page: Page, resolved: ResolvedTarget | None
) -> _PopupSession | None:
    """Replay the step's action. Returns the popup it opened, if any."""

    stage = ctx.stage
    cached = ctx.cached
    if isinstance(cached, CachedAction) and cached.opens_popup and stage.popup is not None:
        raise RenderError("v1 obsługuje co najwyżej jeden popup w całej sesji")
    if ctx.kind == "closeWindow" and stage.popup is None:
        raise RenderError(ctx.message("closeWindow bez otwartego okna"))
    return await _step._render_step(
        active_page,
        _build_recorder(ctx, active_page),
        stage.overlay,
        stage.chrome,
        ctx.plan.scenario,
        ctx.step,
        ctx.kind,
        ctx.index,
        cached,
        stage.anchor,
        stage.observed_pages,
        stage.ensure_card,
        entry=ctx.entry,
        total=ctx.plan.total,
        sensitive=ctx.plan.sensitive_values,
        expect_chrome=stage.expects_bar(active_page),
        resolved=resolved,
        optional=ctx.optional,
        scenario_hash=ctx.plan.scenario_hash,
        on_resolved=ctx.plan.persist_resolved,
    )


async def _furnish_popup(ctx: _StepCtx, opened: _PopupSession) -> None:
    """Adopt a freshly opened popup: decide what it is, mount it, take the cursor."""

    stage = ctx.stage
    stage.popup = opened
    opened.page.set_default_timeout(ctx.opts.timeout * 1000)
    opened.is_blank_tab = not await _popup_window_opened(stage.page)
    opened.wants_bar = stage.chrome is not None and opened.is_blank_tab
    prepared = await _prepare_popup(
        opened.page,
        stage.overlay,
        stage.chrome,
        expect_chrome=stage.expect_chrome or opened.wants_bar,
        mount_bar=opened.wants_bar,
    )
    stage.sync_popup_close()
    if not prepared:
        raise RenderError("popup zamknął się podczas otwierania")
    # The popup now owns the cursor (it mounted its own); stop painting a second
    # one in the main window behind it.
    await _hand_cursor_to_popup(stage.page, opened, stage.overlay)


async def _handle_async_popup_close(ctx: _StepCtx, opened: _PopupSession | None) -> None:
    """A popup that went away during the action, outside ``closeWindow``."""

    popup = ctx.stage.popup
    if popup is None or not popup.page.is_closed() or popup.close_handled:
        return
    if opened is not None or ctx.kind in {"say", "navigate", "wait", "slide"}:
        raise RenderError("popup zamknął się asynchronicznie poza obsługiwaną akcją")
    popup.close_handled = True
    await visuals._prepare_main_after_popup_close(
        ctx.stage.page,
        ctx.stage.overlay,
        ctx.stage.chrome,
        ctx.plan.cfg.cursor.settle,
        restore_cursor_to=popup.main_cursor_pos,
    )


async def _settle_popup_lifecycle(ctx: _StepCtx, opened: _PopupSession | None) -> None:
    """Reconcile the page contract with whatever the action did to the windows."""

    stage = ctx.stage
    if opened is not None:
        await _furnish_popup(ctx, opened)
    if stage.page.is_closed():
        raise RenderError("główne okno zostało zamknięte podczas render")
    stage.sync_popup_close()
    await _handle_async_popup_close(ctx, opened)
    if stage.unexpected_pages():
        raise RenderError(ctx.message("nieoczekiwany popup — uruchom `compile --force`"))


async def _render_one_step(ctx: _StepCtx) -> int | None:
    """Replay one flat step. Returns the branch whose children must be skipped."""

    _assert_pages_intact(ctx, "popup zamknął się poza obsługiwaną akcją scenariusza")
    if ctx.opts.verbose:
        tqdm.write(f"[{ctx.index + 1}/{ctx.plan.total}] {ctx.kind}")
    active_page = await _prepare_visuals(ctx)
    try:
        resolved = await _probe_absence(ctx, active_page)
    except _OptionalAbsent as absent:
        return _note_absent(ctx, absent)
    active_page = await _narrate(ctx)
    try:
        opened = await _perform(ctx, active_page, resolved)
        await _settle_popup_lifecycle(ctx, opened)
    except _OptionalAbsent as absent:
        # Only a cached gate reaches here (its `waitFor` timed out); every other
        # absence signal was already settled by the probe above.
        return _note_absent(ctx, absent)
    except Exception as exc:
        await _fail_step(ctx, exc)
    return None


async def _postroll(stage: _Stage) -> None:
    """One last captured frame, then the end-of-scenario page contract."""

    await asyncio.sleep(_VIDEO_POSTROLL_SECONDS)
    await stage.active_page.screenshot()
    stage.sync_popup_close()
    if stage.page.is_closed():
        raise RenderError("główne okno zostało zamknięte na końcu scenariusza")
    if stage.unexpected_pages():
        raise RenderError("nieoczekiwany popup na końcu scenariusza")
    if stage.popup_closed_unhandled():
        raise RenderError("popup zamknął się asynchronicznie na końcu scenariusza")


async def _run_steps(
    plan: _RenderPlan, stage: _Stage, clock: _Clock, opts: _LoopOptions
) -> None:
    """Replay every flat step, then close the stage — whatever happened."""

    #: branch whose gate turned out to be absent — every step of it is skipped
    skipped_branch: int | None = None
    bar = tqdm(total=plan.total, desc="render", unit="krok", disable=not opts.verbose)
    try:
        for index, entry in enumerate(plan.flat):
            if skipped_branch is not None and entry.branch == skipped_branch:
                # The gate never showed: the branch's children never run, and
                # their narration is removed from the timeline rather than left as
                # silence (segments are placed per index, so never placing them
                # removes them).
                bar.update(1)
                continue
            skipped_branch = await _render_one_step(
                _StepCtx.of(plan, stage, clock, opts, entry, index)
            )
            bar.update(1)
        await _postroll(stage)
    finally:
        bar.close()
        await _close_stage(stage)
