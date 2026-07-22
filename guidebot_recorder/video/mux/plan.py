"""What the three popup modes agree on: the checked interval and the shared cut.

Everything here runs before the presentation mode is even known. The timestamps
are validated against the main recording, the popup's *usable* span is measured
(what its encoder never captured at the start, what its teardown spoiled at the
end), and the single ``[popup_cut]`` trim/tpad chain all three modes consume is
built. The answer is a frozen :class:`PopupPlan`, so no mode graph can re-derive —
or quietly disagree about — any of it.

Kept apart from :mod:`guidebot_recorder.video.mux.compose` because it is the one
part of the composition with no filtergraph in it beyond that single chain: it is
arithmetic and range checks, and both halves read better unmixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .crop import _normalise_popup_crop, detect_teardown_tail
from .probe import _check_sources, _probe_all, _ProbeResult

#: Slack, in seconds, on every comparison of a timestamp with a container
#: duration. Durations are frame-rounded, so an exact comparison would reject a
#: sub-frame overshoot from the monotonic browser clock. The same slack decides
#: whether a pre/tail segment, or a pad at either end of the popup, is worth
#: emitting at all.
_TOLERANCE = 0.05


@dataclass(frozen=True, slots=True)
class PopupPlan:
    """One validated popup interval and the filter chain every mode consumes.

    Frozen because the modes read it and never correct it: ``opened_at`` is
    already shifted by the visual-ready delay, ``popup_crop`` is already
    normalised, and ``popup_filter`` already pays both gaps.
    """

    main: Path
    popup: Path
    out: Path
    opened_at: float
    """On the main recording's clock, already shifted by the visual-ready delay."""
    closed_at: float
    main_duration: float
    rate: float
    """The main recording's average FPS — what the composite modes normalise to."""
    size: tuple[int, int] | None
    """The main recording's frame size, or ``None`` when ffprobe reported none."""
    popup_span: float
    popup_crop: tuple[int, int, int, int] | None
    """Clamped into the recording and snapped to even pixels, or ``None``."""
    popup_filter: str
    """The shared ``[popup_cut]`` chain, hoisted once and consumed by all three
    modes. Only the consumer differs (concat in cut, the scaled overlay in float,
    the full-size sliding overlay in slide)."""
    has_pre: bool
    has_tail: bool


@dataclass(frozen=True, slots=True)
class _CutTiming:
    """How much of the popup recording is usable, and where its gaps are paid."""

    crop: tuple[int, int, int, int] | None
    source_start: float
    cut_duration: float
    startup_gap: float
    tail_gap: float


def _validated_interval(
    opened_at: float,
    closed_at: float,
    visual_ready_delay: float,
) -> tuple[float, float, float]:
    """Coerce the three caller-supplied offsets to floats and range-check them."""
    opened_at = float(opened_at)
    closed_at = float(closed_at)
    visual_ready_delay = float(visual_ready_delay)
    if not all(math.isfinite(value) for value in (opened_at, closed_at, visual_ready_delay)):
        raise ValueError("popup timestamps must be finite")
    if opened_at < 0:
        raise ValueError(f"opened_at must be >= 0, got {opened_at}")
    if closed_at <= opened_at:
        raise ValueError(f"closed_at must be greater than opened_at, got {opened_at}..{closed_at}")
    if visual_ready_delay < 0:
        raise ValueError(f"visual_ready_delay must be >= 0, got {visual_ready_delay}")
    return opened_at, closed_at, visual_ready_delay


def _clamped_to_main(
    opened_at: float,
    closed_at: float,
    visual_ready_delay: float,
    main_duration: float,
) -> tuple[float, float, float]:
    """Fit the interval inside the main recording; return it with its raw span."""
    # Container durations are frame-rounded.  Accept a sub-frame overshoot from
    # the monotonic browser clock, but fail loudly on a genuinely invalid range.
    if opened_at > main_duration + _TOLERANCE:
        raise ValueError(f"opened_at ({opened_at}) is past main video duration ({main_duration})")
    if closed_at > main_duration + _TOLERANCE:
        raise ValueError(f"closed_at ({closed_at}) is past main video duration ({main_duration})")
    opened_at = min(opened_at, main_duration)
    closed_at = min(closed_at, main_duration)
    raw_popup_span = closed_at - opened_at
    if raw_popup_span <= 0:
        raise ValueError("popup interval has no encoded video frames")
    if visual_ready_delay >= raw_popup_span:
        raise ValueError("visual-ready delay consumes the whole popup interval")
    return opened_at, closed_at, raw_popup_span


def _popup_cut_timing(
    popup: Path,
    popup_probe: _ProbeResult,
    popup_crop: tuple[int, int, int, int] | None,
    *,
    raw_popup_span: float,
    popup_span: float,
    source_start: float,
) -> _CutTiming:
    """Measure the usable popup and split the shortfall between its two ends."""
    encoder_startup_gap = max(0.0, raw_popup_span - popup_probe.duration)
    # Page events precede the popup encoder's first frame. Real Chromium startup
    # can take a couple of seconds; permit that floor or 15% on longer intervals,
    # while rejecting a mismatch large enough to describe a different timeline.
    max_startup_gap = max(2.0, raw_popup_span * 0.15)
    if encoder_startup_gap > max_startup_gap:
        raise ValueError(
            f"popup encoder startup gap ({encoder_startup_gap}) exceeds limit ({max_startup_gap})"
        )

    # The mirror image of the prime delay: frames the recording carries at the
    # *end* that no longer match the crop (see ``detect_teardown_tail``). Dropped
    # from the source here and paid back by ``_popup_cut_filter``'s stop-pad,
    # cloning the last good frame, so the composite keeps its length and simply
    # holds the popup a moment longer instead of showing it shrink into filler.
    crop = _normalise_popup_crop(popup_crop, popup_probe.size)
    teardown_tail = (
        detect_teardown_tail(popup, crop, metadata=popup_probe) if crop is not None else 0.0
    )
    popup_recorded = popup_probe.duration - source_start
    popup_available = popup_recorded - teardown_tail
    if popup_span <= 0 or popup_available <= 0:
        raise ValueError("popup has no verified encoded video frames")
    # The startup gap stays measured against what the recording actually holds:
    # it describes frames that were never encoded, and is paid at the *start* by
    # cloning forward. The teardown tail is the opposite — frames that exist but
    # must not be shown — so it is paid at the *end*, cloning the last good frame.
    startup_gap = max(0.0, popup_span - popup_recorded)
    cut_duration = min(popup_span, popup_available)
    return _CutTiming(
        crop=crop,
        source_start=source_start,
        cut_duration=cut_duration,
        startup_gap=startup_gap,
        tail_gap=max(0.0, popup_span - startup_gap - cut_duration),
    )


def _popup_cut_filter(timing: _CutTiming, popup_span: float) -> str:
    """Build the ``[popup_cut]`` chain: trim the source, pay both gaps, re-trim."""
    popup_filter = (
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,"
        f"trim=start={timing.source_start:.6f}:"
        f"end={timing.source_start + timing.cut_duration:.6f},"
        "setpts=PTS-STARTPTS"
    )
    if timing.startup_gap > _TOLERANCE:
        popup_filter += f",tpad=start_mode=clone:start_duration={timing.startup_gap:.6f}"
    if timing.tail_gap > _TOLERANCE:
        popup_filter += f",tpad=stop_mode=clone:stop_duration={timing.tail_gap:.6f}"
    return popup_filter + (
        f",trim=duration={popup_span:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]"
    )


def plan_popup_cut(
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    *,
    visual_ready_delay: float,
    popup_crop: tuple[int, int, int, int] | None,
) -> PopupPlan:
    """Validate one popup interval and derive everything the modes share.

    Reads both recordings exactly once (see :func:`~guidebot_recorder.video.mux.probe._probe_all`
    on why the results are not cached globally), then hands the modes a plan they
    only have to read.
    """
    main, popup, out = Path(main), Path(popup), Path(out)
    _check_sources(main, popup)
    opened_at, closed_at, visual_ready_delay = _validated_interval(
        opened_at, closed_at, visual_ready_delay
    )

    main_probe = _probe_all(main)
    opened_at, closed_at, raw_popup_span = _clamped_to_main(
        opened_at, closed_at, visual_ready_delay, main_probe.duration
    )
    # One probe of the popup, shared by everything below that needs its geometry
    # or its length.
    popup_probe = _probe_all(popup)

    # Playwright resets each WebM's PTS to zero, so container duration cannot
    # reveal which early raw frames preceded the verified visual-ready point.
    # Conservatively trim the full wall-clock prime delay from the source. This
    # may discard a few already-good frames, but guarantees that tpad can only
    # clone a post-prime frame.
    opened_at += visual_ready_delay
    popup_span = closed_at - opened_at
    timing = _popup_cut_timing(
        popup,
        popup_probe,
        popup_crop,
        raw_popup_span=raw_popup_span,
        popup_span=popup_span,
        source_start=visual_ready_delay,
    )
    return PopupPlan(
        main=main,
        popup=popup,
        out=out,
        opened_at=opened_at,
        closed_at=closed_at,
        main_duration=main_probe.duration,
        rate=main_probe.fps,
        size=main_probe.size,
        popup_span=popup_span,
        popup_crop=timing.crop,
        popup_filter=_popup_cut_filter(timing, popup_span),
        has_pre=opened_at > _TOLERANCE,
        has_tail=main_probe.duration - closed_at > _TOLERANCE,
    )
