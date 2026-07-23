"""Post-production: the recording becomes a film, in three named stages.

**Popup composition MUST run before time editing, and the stages are named so
that reads as an ordering rather than as two adjacent paragraphs.**

* :class:`_RecordedFilm` — straight off Playwright, on the **recording** axis. A
  popup's ``opened_at``/``closed_at`` are raw wall clock measured against the same
  anchor, so they are only meaningful here.
* :class:`_ComposedFilm` — the popup framed into the main window's picture. Still
  the recording axis: composition neither adds nor removes frames.
* :class:`_VirtualFilm` — held frames inserted. This is what moves narration and
  SFX onto the **virtual** axis, and it is the last thing that may touch the
  picture.

Swap the two and the film comes out exactly as long as the timeline says, with the
popup at the wrong moment — every downstream guard compares the model against
itself, so all of them stay green. The repo has **no type checker**, so these
classes are readability, not enforcement; the real protection is
``test_popup_is_composed_before_time_editing_and_feeds_it`` (phase 0), which
asserts both the call order and that the edit consumes the compositor's output.

Two test seams are name-imported here because this module calls them:
``compose_popup_video`` and ``probe_frame_count`` — the latter has a second
consumer in :mod:`~guidebot_recorder.recorder.render.timeline`, so replacing it
takes two patch lines. ``_apply_timeline_edits`` and ``_assemble_audio_tracks``
are inside-defined seams, called through their module objects.

``timeline`` is imported as ``timeline_module`` because ``timeline`` is a local
name in the functions below; ``mux_probe`` is aliased for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import FadeSpec, compose_popup_video

# `probe_duration` is a test seam: the mux facade withholds it and the call must
# stay late-bound, so its defining module is imported here instead.
from guidebot_recorder.video.mux import probe as mux_probe
from guidebot_recorder.video.timeline import (
    Timeline,
    assert_recording_fps,
    frames_to_seconds,
    probe_frame_count,
)

from . import audio
from . import timeline as timeline_module
from .clock import _Clock
from .plan import _RenderPlan
from .popup_crop import _popup_fills_canvas, _resolve_popup_crop
from .popup_session import _PopupSession
from .stage import _Stage
from .timeline import _build_timeline


@dataclass(frozen=True, slots=True)
class _Film:
    """A video file plus whether it has already been through an encoder.

    ``preencoded`` is not cosmetic: it tells the muxer the stream is already in
    the output codec and may be copied rather than re-encoded.
    """

    path: Path
    preencoded: bool


#: Straight off Playwright — recording axis, no popup framing, no held frames.
_RecordedFilm = _Film
#: Popup framed in. Still the recording axis: composition changes no frame count.
_ComposedFilm = _Film
#: Held frames inserted — the virtual axis, and the last edit to the picture.
_VirtualFilm = _Film


async def _compose_popup(plan: _RenderPlan, stage: _Stage, film: _RecordedFilm) -> _ComposedFilm:
    """Frame the popup into the main window's picture, on the RECORDING axis."""

    popup = stage.popup
    if popup is None:
        return film
    cfg = plan.cfg
    popup_webm = Path(await popup.video.path())
    # The popup recorded onto the main window's canvas; crop it back to its real
    # window so float frames that and not a viewport-sized rectangle of filler.
    # Three levels, best evidence first; all declining -> today's full canvas.
    popup_crop, _crop_level = _resolve_popup_crop(
        window_size=popup.window_size,
        content_box=popup.content_box,
        popup_video=popup_webm,
        verbose=plan.verbose,
        viewport=popup.viewport,
        canvas=(cfg.viewport.width, cfg.viewport.height),
    )
    transition = _effective_transition(plan, popup, popup_crop)
    closed_at = mux_probe.probe_duration(film.path) if stage.popup_open_at_end else popup.closed_at
    assert closed_at is not None
    composite = plan.work / f"{plan.out_mp4.stem}.composite.mp4"
    compose_popup_video(
        film.path,
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
    return _ComposedFilm(path=composite, preencoded=True)


def _effective_transition(
    plan: _RenderPlan, popup: _PopupSession, popup_crop: tuple[int, int, int, int] | None
) -> str:
    """How the popup is presented, after the full-canvas tab override.

    A real browser TAB that fills the canvas is not a floating popup: `slide` is
    the full-frame presentation by design and ignores `popup_crop`, while `float`
    would inset a whole viewport and read as a shrunken clone of the page. Gated
    on `is_blank_tab`, not on the crop alone — a featureless `window.open`
    painting a full-bleed background also declines every crop level, yet is a
    genuine floating window that must keep `float`. Only `float` is overridden: an
    author who asked for `cut` gets the hard cut they asked for.
    """

    cfg = plan.cfg
    transition = cfg.popup.effective_transition
    if (
        transition == "float"
        and popup.is_blank_tab
        and _popup_fills_canvas(popup_crop, cfg.viewport)
    ):
        transition = "slide"
        if plan.verbose:
            tqdm.write("popup wypełnia kadr — wymuszam przejście `slide` zamiast `float`")
    return transition


def _edit_time(
    plan: _RenderPlan, clock: _Clock, film: _ComposedFilm, *, dump_timeline: bool
) -> tuple[_VirtualFilm, Timeline]:
    """Insert the held frames — the move from the recording axis to the virtual one.

    Runs AFTER popup composition: popups are composed on the recording axis (their
    opened_at/closed_at are raw wall clock) and must stay there. Only what is
    consumed downstream — narration and SFX — moves onto the virtual axis.
    """

    timeline = _build_timeline(clock.time_edits, source_frames=probe_frame_count(film.path))
    if dump_timeline:
        plan.out_mp4.with_suffix(".timeline.json").write_text(timeline.to_json(), encoding="utf-8")
    if timeline.is_empty:
        return film, timeline
    assert_recording_fps(film.path)
    edited = plan.work / f"{plan.out_mp4.stem}.timeline.mp4"
    timeline_module._apply_timeline_edits(film.path, timeline, edited)
    return _VirtualFilm(path=edited, preencoded=True), timeline


async def _lay_audio(
    plan: _RenderPlan, clock: _Clock, film: _VirtualFilm, timeline: Timeline
) -> None:
    """Map every placement onto the virtual axis, then build and publish the beds."""

    cfg = plan.cfg
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
        (kind, frames_to_seconds(timeline.to_virtual(frame)))
        for kind, frame in clock.sfx_frames(cfg.sound)
    ]
    await audio._assemble_audio_tracks(
        film.path,
        plan.audio_configs,
        placed_tracks,
        total,
        plan.work,
        plan.out_mp4,
        preencoded=film.preencoded,
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


async def _publish_film(
    plan: _RenderPlan, stage: _Stage, clock: _Clock, *, dump_timeline: bool
) -> None:
    """Recording -> composed -> virtual -> mastered. The order is the contract."""

    recorded = _RecordedFilm(path=Path(await stage.video.path()), preencoded=False)
    composed = await _compose_popup(plan, stage, recorded)
    virtual, timeline = _edit_time(plan, clock, composed, dump_timeline=dump_timeline)
    await _lay_audio(plan, clock, virtual, timeline)
