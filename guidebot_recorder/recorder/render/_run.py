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

from collections.abc import Mapping
from pathlib import Path

from playwright.async_api import Browser
from tqdm import tqdm

from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.tts.base import TtsProvider
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import FadeSpec, compose_popup_video

# `probe_duration` is a test seam: the mux facade withholds it and the call must
# stay late-bound, so its defining module is imported here instead. Aliased
# because `probe` is already used as a local name elsewhere in this module.
from guidebot_recorder.video.mux import probe as mux_probe
from guidebot_recorder.video.timeline import (
    assert_recording_fps,
    frames_to_seconds,
    probe_frame_count,
)

from . import audio
from . import timeline as timeline_module
from .clock import _Clock
from .loop import _LoopOptions, _run_steps
from .plan import _prepare_render
from .popup_crop import _popup_fills_canvas, _resolve_popup_crop
from .stage import _open_stage
from .timeline import _build_timeline


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
    # Read-only aliases onto the frozen plan, for the post-production phase below.
    cfg = plan.cfg
    out_mp4 = plan.out_mp4
    work = plan.work

    stage = await _open_stage(browser, plan, env=env, timeout=timeout)

    # Audio placements are collected as recording-axis FRAMES, not seconds — see
    # `clock.py` for why, and for why `note_sfx` is handed over as a bound method.
    clock = _Clock.started(stage.anchor, plan.audio_configs)
    await _run_steps(
        plan,
        stage,
        clock,
        _LoopOptions(
            timeout=timeout,
            pause_on_error=pause_on_error,
            verbose=verbose,
            reasoner=reasoner,
        ),
    )

    sfx_frames = clock.sfx_frames(cfg.sound)

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
    timeline = _build_timeline(clock.time_edits, source_frames=probe_frame_count(source_video))
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
        for lang, placed in clock.placed_by_language.items()
    }
    sfx_offsets = [
        (kind, frames_to_seconds(timeline.to_virtual(frame))) for kind, frame in sfx_frames
    ]

    await audio._assemble_audio_tracks(
        source_video,
        plan.audio_configs,
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
