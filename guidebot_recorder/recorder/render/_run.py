"""``run_render``: the whole render pass, top to bottom.

Phase 3 is turning the three interleaved lifetimes this function carried into
three objects; what is left here is the order the phases run in.

* :class:`~guidebot_recorder.recorder.render.plan._RenderPlan` — frozen:
  everything decided before a browser exists;
* :class:`~guidebot_recorder.recorder.render.stage._Stage` — what is on screen
  now: the pages, the injected layers, the popup, the slide card;
* the recording clock — freezes, SFX and narration placements (still inline
  below; it becomes ``_Clock`` in the next step).

**Popup composition MUST run before time editing.** Popups are composed on the
*recording* axis (their ``opened_at``/``closed_at`` are raw wall clock); time
editing is what moves narration and SFX onto the *virtual* axis. Swapping them
yields a film of the right length with the popup in the wrong place, and
``test_popup_is_composed_before_time_editing_and_feeds_it`` (phase 0) asserts both
the call order and that the edit consumes the compositor's output. The other
load-bearing ordering — the role-gated init scripts — now lives in
:mod:`~guidebot_recorder.recorder.render.stage`, in one function whose body *is*
the order.

Every test seam this function drives is called through a module object —
``narration._pace_narration``, ``timeline_module._apply_timeline_edits``,
``audio._assemble_audio_tracks``, ``_step._render_step``,
``visuals._prepare_main_after_popup_close`` — so a patch on the defining submodule
lands on the globals read here. Of the seams defined *outside* the package, the
two overlay constructors moved to ``stage`` with the code that calls them;
``Recorder``, ``compose_popup_video`` and ``probe_frame_count`` are still
name-imported here, which makes *this* module their patch target.

``timeline`` is imported as ``timeline_module`` because ``timeline`` is already a
local name in ``run_render`` — the same reason ``mux_probe`` is aliased below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import partial
from pathlib import Path

from playwright.async_api import Browser, Frame, Page
from tqdm import tqdm

from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.recorder._debug import pause_for_inspection, redact_exception
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import ResolvedTarget
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError
from guidebot_recorder.tts.base import Segment, TtsProvider
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import FadeSpec, compose_popup_video

# `probe_duration` is a test seam: the mux facade withholds it and the call must
# stay late-bound, so its defining module is imported here instead. Aliased
# because `probe` is already used as a local name elsewhere in this module.
from guidebot_recorder.video.mux import probe as mux_probe
from guidebot_recorder.video.timeline import (
    TimeEdit,
    assert_recording_fps,
    frames_to_seconds,
    probe_frame_count,
)

from . import _step, audio, narration, visuals
from . import timeline as timeline_module
from .errors import RenderError, _OptionalAbsent
from .narration import _stamp_frame
from .plan import _prepare_render
from .popup_crop import _popup_fills_canvas, _resolve_popup_crop
from .popup_detect import _popup_window_opened
from .popup_session import _prepare_popup
from .reuse import _resolve_pending_target
from .stage import _close_stage, _open_stage
from .timeline import _build_timeline
from .visuals import _hand_cursor_to_popup, _play_desktop_opener

_VIDEO_POSTROLL_SECONDS = 0.1


async def run_render(
    path: Path | str,
    out_mp4: Path | str,
    tts_provider: TtsProvider,
    cache_dir: Path | str,
    browser: Browser,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    pause_on_error: bool = False,
    verbose: bool = False,
    hold_frame: bool | None = None,
    hold_frame_settle: float | None = None,
    dump_timeline: bool = False,
    reasoner: Reasoner | None = None,
) -> None:
    plan = await _prepare_render(
        path,
        out_mp4,
        tts_provider,
        cache_dir,
        env=env,
        hold_frame=hold_frame,
        hold_frame_settle=hold_frame_settle,
        verbose=verbose,
    )
    # Read-only aliases onto the frozen plan. The plan is the single source of
    # truth; these exist so the phases below still read as prose rather than as
    # `plan.` repeated three hundred times.
    cfg = plan.cfg
    scenario = plan.scenario
    flat = plan.flat
    compiled = plan.compiled
    audio_configs = plan.audio_configs
    segments = plan.segments
    sensitive_values = plan.sensitive_values
    scenario_hash = plan.scenario_hash
    desktop_payloads = plan.desktop_payloads
    step_message = plan.step_message
    out_mp4 = plan.out_mp4
    work = plan.work

    stage = await _open_stage(browser, plan, env=env, timeout=timeout)

    # Audio placements are collected as recording-axis FRAMES, not seconds: the
    # grid is what `Timeline` reasons on, and quantising once at the moment of
    # observation is what lets `_stamp_frame` keep them monotonic against the
    # freezes. Seconds reappear only at the very end, when the audio bed is built.
    sfx_events: list[tuple[str, int]] = []
    # Freezes recorded while rendering, on the *recording* axis. Applied to the
    # video — and used to remap every audio offset — once the loop is done.
    time_edits: list[TimeEdit] = []
    # Frame of the most recent freeze, or -1 before any. Read by `_stamp_frame`
    # so nothing is ever stamped inside a hold that was already recorded.
    last_freeze_frame = -1

    def sfx_sink(kind: str) -> None:
        sfx_events.append((kind, _stamp_frame(stage.anchor, not_before=last_freeze_frame + 1)))

    placed_by_language: dict[str, list[tuple[Segment, int]]] = {
        tts.lang: [] for tts in audio_configs
    }
    #: branch whose gate turned out to be absent — every step of it is skipped
    skipped_branch: int | None = None

    bar = tqdm(total=len(flat), desc="render", unit="krok", disable=not verbose)
    try:
        for index, entry in enumerate(flat):
            step = entry.step
            if skipped_branch is not None and entry.branch == skipped_branch:
                # The gate never showed: the branch's children never run, and their
                # narration is removed from the timeline rather than left as silence
                # (segments are placed per index, so never placing them removes them).
                bar.update(1)
                continue
            skipped_branch = None
            stage.sync_popup_close()
            if stage.popup_closed_unhandled():
                raise RenderError("popup zamknął się poza obsługiwaną akcją scenariusza")
            if stage.unexpected_pages():
                raise RenderError(
                    step_message(entry, index, "nieoczekiwany popup — uruchom `compile --force`")
                )
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(flat)}] {kind}")

            active_page = stage.active_page
            await active_page.bring_to_front()
            # Card-aware visual prep, ahead of the narration block: a `slide`
            # step paints (replacing any prior card); a `say` step keeps a live
            # card up while it narrates; any other step dismisses the card
            # first (asserting it survived, fail-loud) before its normal
            # `_ensure_visuals`. With no card ever painted this is exactly
            # today's unconditional `_ensure_visuals` call (back-compat).
            if kind == "desktop":
                assert step.desktop is not None  # guaranteed by command_kind()
                if stage.card is not None:
                    await stage.hide_card(active_page)
                await _play_desktop_opener(
                    stage.desktop,
                    stage.overlay,
                    active_page,
                    desktop_payloads[index],
                    hold=step.desktop.hold,
                    settle_ms=cfg.cursor.settle,
                    reveal=partial(stage.chrome_show, active_page),
                    on_click=(sfx_sink if cfg.sound.enabled else None),
                )
                # The opener ends on the revealed chrome shell — normal visible
                # state, so from here it is exactly the no-card path (`card`
                # stays None).
            elif kind == "slide":
                assert step.slide is not None  # guaranteed by command_kind()
                if stage.card is not None:
                    # Fail loud before repainting: a slide following a say whose
                    # card was destroyed mid-narration must NOT silently swap in a
                    # fresh card over the wrong page (`reveal_page` asserts the
                    # token, exactly like the generic dismiss branch below).
                    await stage.reveal_page(active_page)
                await stage.show_card(
                    active_page,
                    {
                        "title": step.slide.title,
                        "subtitle": step.slide.subtitle,
                        "notes": step.slide.notes,
                    },
                )
            elif kind == "say" and stage.card is not None:
                await stage.ensure_card(active_page)
            elif stage.card is not None:
                await stage.reveal_page(active_page)
                await stage.ensure_visuals(active_page, expect_chrome=stage.expect_chrome)
            else:
                await stage.ensure_visuals(active_page, expect_chrome=stage.expect_chrome)

            # --- absence probe / in-place resolution, ahead of the narration ----
            # An optional step that turns out to be absent must not narrate first
            # and only then do nothing, so everything decidable before the action —
            # a stale frozen target, an unresolvable pending entry — is decided
            # here. A cached gate is the exception: its `waitFor` IS the action, so
            # it stays in `_step._render_step` (a synthetic gate step never
            # narrates).
            #
            # `optional` marks the only two places absence is tolerated: a branch
            # gate and an `optional: true` step. A *child* of an entered branch is
            # not optional — the branch demonstrably happened, so anything failing
            # inside it is a real regression (§5 of the design). Its pending entry
            # is still resolved here; only the verdict on absence differs.
            optional = entry.is_gate or step.optional
            resolved: ResolvedTarget | None = None
            cached = compiled.actions[index]
            # The site iframe for the main window, the page itself for popups /
            # chrome-disabled renders — never the shell document, which the shim
            # deliberately skips.
            probe_root: Page | Frame = (
                stage.site_frame
                if active_page is stage.page and stage.site_frame is not None
                else active_page
            )
            if stage.selects is not None and step.requires_target():
                # Readiness barrier, the mirror of compile's: both the in-place
                # resolution below and the frozen-target check inside
                # ``_render_step`` must see the shimmed DOM, or render would drive
                # a page compile never resolved against. Any navigation that led
                # here has settled — it was an earlier step.
                try:
                    await stage.selects.wait_ready(probe_root)
                except SelectsNotReadyError as exc:
                    # The barrier sits outside every per-step ``except`` in this
                    # loop, so without this the one failure that stops a render
                    # before its step even begins would be the only one to reach
                    # the author with no file, no line and no YAML fragment.
                    raise RenderError(step_message(entry, index, str(exc))) from exc
            if step.requires_target() and (optional or isinstance(cached, PendingAction)):
                try:
                    if isinstance(cached, PendingAction):
                        if reasoner is None:
                            raise _OptionalAbsent(
                                "brak dostępnego reasonera, a krok nie został skompilowany "
                                "(pending) — zainstaluj `codex`, aby rozwiązać go na miejscu"
                            )
                        resolved = await _resolve_pending_target(probe_root, step, kind, reasoner)
                    elif isinstance(cached, CachedAction) and cached.action != "waitFor":
                        if not await reuse_is_valid(probe_root, cached):
                            raise _OptionalAbsent("zamrożony namiar nie pasuje do strony")
                except _OptionalAbsent as absent:
                    if not optional:
                        raise RenderError(step_message(entry, index, str(absent))) from None
                    plan.note_skip(entry, index, str(absent), gate=entry.is_gate)
                    if entry.is_gate:
                        skipped_branch = entry.branch
                    bar.update(1)
                    continue
                except Exception as exc:
                    # Everything the resolver rejects for a reason other than
                    # absence — `multiple_actions` above all — is an authoring bug
                    # and fails the render, exactly as in the action loop below.
                    safe_message = redact_exception(exc, sensitive_values)
                    if verbose:
                        tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
                    if pause_on_error:
                        await pause_for_inspection(
                            stage.active_page,
                            "render",
                            index,
                            kind,
                            exc,
                            sensitive_values,
                            total=len(flat),
                            location=entry.location,
                            source=scenario.source,
                        )
                    raise RenderError(f"{type(exc).__name__}: {safe_message}") from None

            step_segments: list[Segment] = []
            # Recording-axis frame: the mapping onto the finished film needs the
            # complete edit list, which does not exist until the loop ends.
            narration_frame = _stamp_frame(stage.anchor, not_before=last_freeze_frame + 1)
            for tts in audio_configs:
                seg = segments[tts.lang].get(index)
                if seg is not None:
                    placed_by_language[tts.lang].append((seg, narration_frame))
                    step_segments.append(seg)
            if step_segments:
                # One picture timeline: the action waits for the longest language,
                # while shorter tracks naturally contain silence before the action.
                emitted = await narration._pace_narration(
                    step_segments,
                    anchor=stage.anchor,
                    hold_frame=cfg.hold_frame_for_narration,
                    settle=cfg.hold_frame_settle,
                    edits=time_edits,
                    not_before=narration_frame,
                )
                if emitted is not None:
                    last_freeze_frame = emitted

            stage.sync_popup_close()
            if stage.popup_closed_unhandled():
                raise RenderError("popup zamknął się asynchronicznie podczas narracji")
            active_page = stage.active_page
            if stage.unexpected_pages():
                raise RenderError(
                    step_message(entry, index, "nieoczekiwany popup — uruchom `compile --force`")
                )
            await active_page.bring_to_front()
            # Card-aware post-narration re-assert: a navigation that destroyed the
            # card DURING the narration wait (a say/slide over a live card) must
            # fail loud here — this is the checkpoint that catches a mid-wait
            # destruction even when the say is the LAST step (the loop still fully
            # processes that step before exiting). When no card is active this is
            # exactly today's unconditional `_ensure_visuals` (back-compat).
            if stage.card is not None:
                await stage.ensure_card(active_page)
            else:
                await stage.ensure_visuals(
                    active_page, expect_chrome=stage.expects_bar(active_page)
                )
            if isinstance(cached, CachedAction) and cached.opens_popup and stage.popup is not None:
                raise RenderError("v1 obsługuje co najwyżej jeden popup w całej sesji")
            if kind == "closeWindow" and stage.popup is None:
                raise RenderError(step_message(entry, index, "closeWindow bez otwartego okna"))
            # Main window drives the site iframe (a Frame); popups drive the page.
            on_shell = active_page is stage.page and stage.site_frame is not None
            recorder = Recorder(
                active_page,
                stage.overlay,
                settle_ms=cfg.cursor.settle,
                frame=stage.site_frame if on_shell else None,
                type_delay_ms=(cfg.typing.speed if cfg.typing.animate else None),
                type_jitter_ms=cfg.typing.jitter_ms,
                type_max_delay_factor=cfg.typing.max_delay_factor,
                on_sfx=(sfx_sink if cfg.sound.enabled else None),
                # How long the unfurled option list is held before the cursor
                # sets off towards the chosen row. Render is the only phase that
                # animates a `select:` step, so this is the one place the
                # configured value can take effect at all.
                open_hold_ms=cfg.selects.open_hold_ms,
            )
            try:
                opened = await _step._render_step(
                    active_page,
                    recorder,
                    stage.overlay,
                    stage.chrome,
                    scenario,
                    step,
                    kind,
                    index,
                    cached,
                    stage.anchor,
                    stage.observed_pages,
                    stage.ensure_card,
                    entry=entry,
                    total=len(flat),
                    sensitive=sensitive_values,
                    expect_chrome=stage.expects_bar(active_page),
                    resolved=resolved,
                    optional=optional,
                    scenario_hash=scenario_hash,
                    on_resolved=plan.persist_resolved,
                )
                if opened is not None:
                    stage.popup = opened
                    opened.page.set_default_timeout(timeout * 1000)
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
                    # The popup now owns the cursor (it mounted its own); stop
                    # painting a second one in the main window behind it.
                    await _hand_cursor_to_popup(stage.page, opened, stage.overlay)
                if stage.page.is_closed():
                    raise RenderError("główne okno zostało zamknięte podczas render")
                stage.sync_popup_close()
                if stage.popup is not None and stage.popup.page.is_closed():
                    if not stage.popup.close_handled:
                        if opened is not None or kind in {"say", "navigate", "wait", "slide"}:
                            raise RenderError(
                                "popup zamknął się asynchronicznie poza obsługiwaną akcją"
                            )
                        stage.popup.close_handled = True
                        await visuals._prepare_main_after_popup_close(
                            stage.page,
                            stage.overlay,
                            stage.chrome,
                            cfg.cursor.settle,
                            restore_cursor_to=stage.popup.main_cursor_pos,
                        )
                if stage.unexpected_pages():
                    raise RenderError(
                        step_message(
                            entry, index, "nieoczekiwany popup — uruchom `compile --force`"
                        )
                    )
            except _OptionalAbsent as absent:
                # Only a cached gate reaches here (its `waitFor` timed out); every
                # other absence signal was already settled by the probe above.
                plan.note_skip(entry, index, str(absent), gate=entry.is_gate)
                if entry.is_gate:
                    skipped_branch = entry.branch
            except Exception as exc:
                safe_message = redact_exception(exc, sensitive_values)
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
                if pause_on_error:
                    await pause_for_inspection(
                        stage.active_page,
                        "render",
                        index,
                        kind,
                        exc,
                        sensitive_values,
                        total=len(flat),
                        location=entry.location,
                        source=scenario.source,
                    )
                raise RenderError(f"{type(exc).__name__}: {safe_message}") from None
            bar.update(1)
        # Force a bounded final frame after narration/action completion. Without
        # this post-roll, a static last page can leave the VFR recording a fraction
        # shorter than the audio timeline and make the final syllable trimmable.
        await asyncio.sleep(_VIDEO_POSTROLL_SECONDS)
        postroll_page = stage.active_page
        await postroll_page.screenshot()
        stage.sync_popup_close()
        if stage.page.is_closed():
            raise RenderError("główne okno zostało zamknięte na końcu scenariusza")
        if stage.unexpected_pages():
            raise RenderError("nieoczekiwany popup na końcu scenariusza")
        if stage.popup_closed_unhandled():
            raise RenderError("popup zamknął się asynchronicznie na końcu scenariusza")
    finally:
        bar.close()
        await _close_stage(stage)

    sfx_frames: list[tuple[str, int]] = []
    if cfg.sound.enabled:
        for kind, frame in sfx_events:
            if kind == "click" and not cfg.sound.click:
                continue
            if kind == "key" and not cfg.sound.keys:
                continue
            if frame < 0:
                raise RenderError(f"ujemna klatka SFX ({frame}) — błąd zegara renderu")
            sfx_frames.append((kind, frame))

    main_webm = Path(await stage.video.path())
    popup = stage.popup
    if popup is None:
        source_video = main_webm
        preencoded = False
    else:
        popup_webm = Path(await popup.video.path())
        # The popup recorded onto the main window's canvas; crop it back to its
        # real window so float frames that and not a viewport-sized rectangle of
        # filler. Three levels, best evidence first; all declining -> today's
        # full canvas.
        popup_crop, _crop_level = _resolve_popup_crop(
            window_size=popup.window_size,
            content_box=popup.content_box,
            popup_video=popup_webm,
            verbose=verbose,
            viewport=popup.viewport,
            canvas=(cfg.viewport.width, cfg.viewport.height),
        )
        # A real browser TAB that fills the canvas is not a floating popup:
        # `slide` is the full-frame presentation by design and ignores
        # `popup_crop`, while `float` would inset a whole viewport and read as a
        # shrunken clone of the page. Gated on `is_blank_tab`, not on the crop
        # alone — a featureless `window.open` painting a full-bleed background
        # also declines every crop level, yet is a genuine floating window that
        # must keep `float`. Only `float` is overridden: an author who asked for
        # `cut` gets the hard cut they asked for.
        transition = cfg.popup.effective_transition
        if (
            transition == "float"
            and popup.is_blank_tab
            and _popup_fills_canvas(popup_crop, cfg.viewport)
        ):
            transition = "slide"
            if verbose:
                tqdm.write("popup wypełnia kadr — wymuszam przejście `slide` zamiast `float`")
        closed_at = (
            mux_probe.probe_duration(main_webm) if stage.popup_open_at_end else popup.closed_at
        )
        assert closed_at is not None
        composite = work / f"{out_mp4.stem}.composite.mp4"
        compose_popup_video(
            main_webm,
            popup_webm,
            composite,
            popup.opened_at,
            closed_at,
            visual_ready_delay=popup.visual_ready_delay,
            transition=transition,
            slide_ms=cfg.popup.slide_ms,
            scale=cfg.popup.scale,
            corner_radius=cfg.popup.corner_radius,
            shadow=cfg.popup.shadow,
            backdrop_dim=cfg.popup.backdrop_dim,
            backdrop_blur=cfg.popup.backdrop_blur,
            open_ms=cfg.popup.open_ms,
            close_ms=cfg.popup.close_ms,
            hold_open_at_end=stage.popup_open_at_end,
            popup_crop=popup_crop,
        )
        source_video = composite
        preencoded = True

    # Time editing runs AFTER popup composition: popups are composed on the
    # recording axis (their opened_at/closed_at are raw wall clock) and must stay
    # there. Only what is consumed downstream — narration and SFX — moves onto
    # the virtual axis.
    timeline = _build_timeline(time_edits, source_frames=probe_frame_count(source_video))
    if dump_timeline:
        out_mp4.with_suffix(".timeline.json").write_text(timeline.to_json(), encoding="utf-8")
    if not timeline.is_empty:
        assert_recording_fps(source_video)
        edited = work / f"{out_mp4.stem}.timeline.mp4"
        timeline_module._apply_timeline_edits(source_video, timeline, edited)
        source_video = edited
        preencoded = True

    # Taken from the model rather than probed, which is what makes the audio and
    # video axes agree by construction.
    total = timeline.virtual_duration
    # The one place frames become seconds: mapped on the grid, then converted.
    placed_tracks = {
        lang: [
            Placed(segment=seg, offset=frames_to_seconds(timeline.to_virtual(frame)))
            for seg, frame in placed
        ]
        for lang, placed in placed_by_language.items()
    }
    sfx_offsets = [
        (kind, frames_to_seconds(timeline.to_virtual(frame))) for kind, frame in sfx_frames
    ]

    await audio._assemble_audio_tracks(
        source_video,
        audio_configs,
        placed_tracks,
        total,
        work,
        out_mp4,
        preencoded=preencoded,
        sound=cfg.sound,
        sfx_offsets=sfx_offsets,
        fade=(
            FadeSpec(
                fade_in=cfg.fade.fade_in,
                fade_out=cfg.fade.fade_out,
                color=cfg.fade.color,
                audio=cfg.fade.audio,
            )
            if cfg.fade.enabled
            else None
        ),
    )
