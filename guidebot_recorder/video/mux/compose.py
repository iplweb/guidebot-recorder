"""The popup composition entry point: validation, the shared cut, mode dispatch.

:func:`compose_popup_video` owns everything the three presentation modes have in
common — timestamp validation against the main recording, the encoder-startup and
teardown-tail bookkeeping, and the single ``[popup_cut]`` trim/tpad chain all
three consume. It then either emits the hard cut itself or hands the shared work
to :mod:`guidebot_recorder.video.mux.floating` or
:mod:`guidebot_recorder.video.mux.slide`, which assemble their own filtergraphs.

The two composite modules are separate files rather than one: together with this
one they are the bulk of the package, and each is a self-contained filtergraph
whose comments only make sense next to their own filter chain.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

from . import ffmpeg
from .crop import _normalise_popup_crop, detect_teardown_tail
from .ffmpeg import ffmpeg_bin
from .floating import _compose_floating
from .probe import _check_sources, _probe_all
from .slide import _compose_slide


def compose_popup_video(
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    *,
    visual_ready_delay: float = 0.0,
    transition: Literal["cut", "float", "slide"] | None = None,
    floating: bool = False,
    scale: float = 0.72,
    corner_radius: int = 14,
    shadow: bool = True,
    backdrop_dim: float = 0.45,
    backdrop_blur: int = 0,
    open_ms: int = 320,
    close_ms: int = 240,
    hold_open_at_end: bool = False,
    slide_ms: int = 400,
    popup_crop: tuple[int, int, int, int] | None = None,
) -> None:
    """Cut between the main-page and popup recordings on one timeline.

    ``opened_at`` and ``closed_at`` are offsets on the main recording's clock.
    ``visual_ready_delay`` is the bounded time from the page event until a frame
    with both visual layers was verified. The resulting picture keeps main on
    screen through that delay, trims any earlier popup frames, then follows the
    popup until ``closed_at`` and returns to main. When ``closed_at`` is the end
    of ``main`` the final segment is omitted.

    Playwright gives every page in the context the same configured frame size,
    so no scaling is applied.  Each segment has its timestamps reset before the
    concat filter and the final stream is encoded once as H.264 for MP4.

    ``transition`` selects the presentation mode explicitly and wins over the
    deprecated ``floating`` alias: ``mode = transition if transition is not None
    else ("float" if floating else "cut")``. ``cut`` is the hard cut above;
    ``float`` is the composite below; ``slide`` pushes the popup in as a
    full-frame window (main translates left and exits while the popup enters from
    the right) over ``slide_ms``, holds full-frame, then pushes right on close.

    When the mode is ``float`` the popup interval is not a hard cut but a
    composite: the main page stays on screen (dimmed by ``backdrop_dim``, with an
    optional ``backdrop_blur``) while the popup is drawn as a centred,
    ``scale``-d, rounded-corner (``corner_radius``) window with a drop
    ``shadow``, fading in over ``open_ms`` and out over ``close_ms``. The
    backdrop is normalised to CFR before the split so a backgrounded main page
    (which may emit zero frames during the interval) still fills the whole
    span. When ``hold_open_at_end`` is true the close fade / un-dim (float) or the
    push-out (slide) is skipped and the popup is held to the last frame. All
    cosmetics have defaults, so existing ``floating=False`` callers are unaffected.

    ``popup_crop`` is ``(width, height, x, y)`` in the popup recording's pixels:
    the popup's *real* window inside the recorded frame. Playwright's
    ``record_video_size`` is context-level, so a popup records onto a canvas the
    size of the main viewport with filler around its actual window; without a
    crop the ``float`` mode would frame that whole canvas. It applies to
    ``float`` only (``cut``/``slide`` show the popup full-frame by design) and is
    optional — omit it and the filtergraph is byte-identical to before.
    """
    main, popup, out = Path(main), Path(popup), Path(out)
    _check_sources(main, popup)

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

    main_probe = _probe_all(main)
    main_duration = main_probe.duration
    # Container durations are frame-rounded.  Accept a sub-frame overshoot from
    # the monotonic browser clock, but fail loudly on a genuinely invalid range.
    tolerance = 0.05
    if opened_at > main_duration + tolerance:
        raise ValueError(f"opened_at ({opened_at}) is past main video duration ({main_duration})")
    if closed_at > main_duration + tolerance:
        raise ValueError(f"closed_at ({closed_at}) is past main video duration ({main_duration})")
    opened_at = min(opened_at, main_duration)
    closed_at = min(closed_at, main_duration)
    raw_popup_span = closed_at - opened_at
    if raw_popup_span <= 0:
        raise ValueError("popup interval has no encoded video frames")
    if visual_ready_delay >= raw_popup_span:
        raise ValueError("visual-ready delay consumes the whole popup interval")
    # One probe of the popup, shared by everything below that needs its geometry
    # or its length — see `probe._probe_all` on why results are not cached
    # globally.
    popup_probe = _probe_all(popup)
    popup_duration = popup_probe.duration
    encoder_startup_gap = max(0.0, raw_popup_span - popup_duration)
    # Page events precede the popup encoder's first frame. Real Chromium startup
    # can take a couple of seconds; permit that floor or 15% on longer intervals,
    # while rejecting a mismatch large enough to describe a different timeline.
    max_startup_gap = max(2.0, raw_popup_span * 0.15)
    if encoder_startup_gap > max_startup_gap:
        raise ValueError(
            f"popup encoder startup gap ({encoder_startup_gap}) exceeds limit ({max_startup_gap})"
        )

    # Playwright resets each WebM's PTS to zero, so container duration cannot
    # reveal which early raw frames preceded the verified visual-ready point.
    # Conservatively trim the full wall-clock prime delay from the source. This
    # may discard a few already-good frames, but guarantees that tpad can only
    # clone a post-prime frame.
    popup_source_start = visual_ready_delay
    opened_at += visual_ready_delay
    popup_span = closed_at - opened_at
    # The mirror image of the prime delay above: frames the recording carries at
    # the *end* that no longer match the crop (see ``detect_teardown_tail``).
    # Dropped from the source and paid back below by cloning the last good frame,
    # so the composite keeps its length and simply holds the popup a moment
    # longer instead of showing it shrink into the filler.
    normalised_crop = _normalise_popup_crop(popup_crop, popup_probe.size)
    teardown_tail = (
        detect_teardown_tail(popup, normalised_crop, probe=popup_probe)
        if normalised_crop is not None
        else 0.0
    )
    popup_recorded = popup_duration - popup_source_start
    popup_available = popup_recorded - teardown_tail
    if popup_span <= 0 or popup_available <= 0:
        raise ValueError("popup has no verified encoded video frames")
    # The startup gap stays measured against what the recording actually holds:
    # it describes frames that were never encoded, and is paid at the *start* by
    # cloning forward. The teardown tail is the opposite — frames that exist but
    # must not be shown — so it is paid at the *end*, cloning the last good frame.
    startup_gap = max(0.0, popup_span - popup_recorded)
    popup_cut_duration = min(popup_span, popup_available)
    tail_gap = max(0.0, popup_span - startup_gap - popup_cut_duration)

    has_pre = opened_at > tolerance
    has_tail = main_duration - closed_at > tolerance

    # The reused popup cut: identical trim/tpad math, hoisted once and shared by
    # all three modes. Only the consumer differs (concat in cut, the scaled
    # overlay in float, the full-size sliding overlay in slide).
    popup_filter = (
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,"
        f"trim=start={popup_source_start:.6f}:"
        f"end={popup_source_start + popup_cut_duration:.6f},"
        "setpts=PTS-STARTPTS"
    )
    if startup_gap > tolerance:
        popup_filter += f",tpad=start_mode=clone:start_duration={startup_gap:.6f}"
    if tail_gap > tolerance:
        popup_filter += f",tpad=stop_mode=clone:stop_duration={tail_gap:.6f}"
    popup_filter += (
        f",trim=duration={popup_span:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]"
    )

    mode = transition if transition is not None else ("float" if floating else "cut")

    if mode == "float":
        _compose_floating(
            popup_crop=normalised_crop,
            main=main,
            popup=popup,
            out=out,
            opened_at=opened_at,
            closed_at=closed_at,
            main_duration=main_duration,
            popup_span=popup_span,
            popup_filter=popup_filter,
            has_pre=has_pre,
            has_tail=has_tail,
            scale=scale,
            corner_radius=corner_radius,
            shadow=shadow,
            backdrop_dim=backdrop_dim,
            backdrop_blur=backdrop_blur,
            open_ms=open_ms,
            close_ms=close_ms,
            hold_open_at_end=hold_open_at_end,
            rate=main_probe.fps,
        )
        return

    if mode == "slide":
        _compose_slide(
            main=main,
            popup=popup,
            out=out,
            opened_at=opened_at,
            closed_at=closed_at,
            main_duration=main_duration,
            popup_span=popup_span,
            popup_filter=popup_filter,
            has_pre=has_pre,
            has_tail=has_tail,
            slide_ms=slide_ms,
            hold_open_at_end=hold_open_at_end,
            rate=main_probe.fps,
            size=main_probe.size,
        )
        return

    filters: list[str] = []
    main_sources: dict[str, str] = {}
    if has_pre and has_tail:
        filters.append("[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]")
        main_sources = {"pre": "[main_pre_src]", "tail": "[main_tail_src]"}
    elif has_pre:
        main_sources = {"pre": "[0:v]"}
    elif has_tail:
        main_sources = {"tail": "[0:v]"}

    labels: list[str] = []
    if has_pre:
        source = main_sources["pre"]
        normalize = "" if has_tail else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start=0:end={opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )
        labels.append("[main_pre]")

    # The shared [popup_cut] built once above (hoisted for cut/float/slide).
    filters.append(popup_filter)
    labels.append("[popup_cut]")

    if has_tail:
        source = main_sources["tail"]
        normalize = "" if has_pre else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start={closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )
        labels.append("[main_tail]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}null[outv]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

    ffmpeg._run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(main),
            "-i",
            str(popup),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ],
        out,
    )
