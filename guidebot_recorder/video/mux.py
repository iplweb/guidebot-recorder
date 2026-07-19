"""ffprobe, FFmpeg video assembly, and audio muxing helpers.

All helpers are fail-loud: a missing binary or a non-zero exit raises immediately
(no silent fallbacks, per the design's fail-loud rule).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from guidebot_recorder.languages import is_iso_639_2

#: Audio sample rate used everywhere in the montage pipeline (design §8).
SAMPLE_RATE = 48000


@dataclass(frozen=True, slots=True)
class MuxAudioTrack:
    """One audio input and the metadata of its MP4 stream."""

    path: Path
    language: str
    title: str | None = None
    default: bool = False


def _resolve(binary: str) -> str:
    """Return the absolute path to *binary* or raise a loud error."""
    found = shutil.which(binary)
    if found is None:
        raise RuntimeError(
            f"'{binary}' not found on PATH. Install ffmpeg "
            "(e.g. `brew install ffmpeg`) to render/mux video."
        )
    return found


def ffmpeg_bin() -> str:
    """Path to the ffmpeg executable (fail-loud if absent)."""
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    """Path to the ffprobe executable (fail-loud if absent)."""
    return _resolve("ffprobe")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output; raise ``RuntimeError`` on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _run_to_output(cmd: list[str], out: Path) -> None:
    """Run an ffmpeg command atomically, appending a temporary output path.

    The temporary file lives beside the final artifact so ``os.replace`` is atomic.
    A failed command never truncates a previously successful MP4/WAV.
    """

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{out.stem}.",
        suffix=out.suffix,
        dir=out.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _run([*cmd, str(temporary)])
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)


def probe_duration(path: Path) -> float:
    """Return the container duration of *path* in seconds via ffprobe.

    Raises ``FileNotFoundError`` if the file is missing and ``RuntimeError`` if
    ffprobe cannot report a numeric duration.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    raw = proc.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"ffprobe returned non-numeric duration: {raw!r}") from exc


def _check_sources(*paths: Path) -> None:
    """Raise before invoking ffmpeg when any input is missing."""
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)


def _probe_fps(path: Path, default: float = 25.0) -> float:
    """Return the average frame rate of *path*'s first video stream.

    Playwright screencasts are VFR, so ``avg_frame_rate`` can be a coarse
    ``num/den`` ratio (or ``0/0`` for a degenerate stream). Falls back to
    *default* whenever ffprobe cannot report a usable positive rate. The value
    only picks the CFR grid the floating backdrop is normalised onto.
    """
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    raw = proc.stdout.strip()
    try:
        num, _, den = raw.partition("/")
        rate = float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        return default
    return rate if rate > 0 else default


def _probe_size(path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of *path*'s first video stream in pixels.

    The slide compositor needs concrete dimensions for the CFR ``color`` base the
    two overlays tile across (``overlay``'s ``W`` variable then references this
    base width in the push expressions). Fail loud if ffprobe cannot report them.
    """
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ]
    )
    raw = proc.stdout.strip()
    try:
        width_str, height_str = raw.split("x")
        return int(width_str), int(height_str)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"ffprobe returned non-numeric size: {raw!r}") from exc


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

    main_duration = probe_duration(main)
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
    popup_duration = probe_duration(popup)
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
    popup_available = popup_duration - popup_source_start
    if popup_span <= 0 or popup_available <= 0:
        raise ValueError("popup has no verified encoded video frames")
    startup_gap = max(0.0, popup_span - popup_available)
    popup_cut_duration = min(popup_span, popup_available)

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
    popup_filter += (
        f",trim=duration={popup_span:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]"
    )

    mode = transition if transition is not None else ("float" if floating else "cut")

    if mode == "float":
        _compose_floating(
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

    _run_to_output(
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


def _compose_floating(
    *,
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    main_duration: float,
    popup_span: float,
    popup_filter: str,
    has_pre: bool,
    has_tail: bool,
    scale: float,
    corner_radius: int,
    shadow: bool,
    backdrop_dim: float,
    backdrop_blur: int,
    open_ms: int,
    close_ms: int,
    hold_open_at_end: bool,
) -> None:
    """Assemble and run the floating-popup composite filtergraph.

    Shares the caller's validated trim math (``opened_at``/``closed_at`` already
    shifted by the visual-ready delay, ``popup_filter`` the reused popup cut).
    The main input is CFR-normalised (``fps``) *before* the 3-way split so the
    always-consumed middle segment (``main[opened_at:closed_at]``) fills the
    whole span even when the backgrounded main page emitted no frames there.
    """

    span = popup_span
    open_eff = min(open_ms / 1000.0, span / 2.0)
    close_eff = min(close_ms / 1000.0, span - open_eff)
    rate = _probe_fps(main)

    filters: list[str] = []

    # --- CFR normalise, then 3-way split (mid is ALWAYS consumed) -------------
    split_targets: list[str] = []
    if has_pre:
        split_targets.append("[main_pre_src]")
    split_targets.append("[main_mid_src]")
    if has_tail:
        split_targets.append("[main_tail_src]")
    main_norm = f"[0:v]fps={rate:.6f},settb=AVTB,setpts=PTS-STARTPTS"
    if len(split_targets) == 1:
        filters.append(f"{main_norm}[main_mid_src]")
    else:
        filters.append(f"{main_norm},split={len(split_targets)}{''.join(split_targets)}")

    # --- pre (verbatim main) --------------------------------------------------
    if has_pre:
        filters.append(
            f"[main_pre_src]trim=start=0:end={opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )

    # --- dimmed backdrop (ramps with the fade so it darkens in step) ----------
    # ``open_ms=0`` (a valid "no open animation" config) makes open_eff 0; guard
    # the division so the eq expression never becomes t/0 (inf/NaN brightness).
    rise = "1" if open_eff <= 0 else f"min(1,t/{open_eff:.6f})"
    if hold_open_at_end or close_eff <= 0:
        ramp = rise
    else:
        fall = f"min(1,({span:.6f}-t)/{close_eff:.6f})"
        ramp = f"min({rise},{fall})"
    dim_expr = f"-{backdrop_dim:.6f}*{ramp}"
    backdrop = (
        f"[main_mid_src]trim=start={opened_at:.6f}:end={closed_at:.6f},"
        f"setpts=PTS-STARTPTS,eq=brightness='{dim_expr}':eval=frame"
    )
    if backdrop_blur > 0:
        backdrop += f",boxblur={backdrop_blur}"
    backdrop += ",setsar=1,format=yuv420p[dim]"
    filters.append(backdrop)

    # --- the reused popup cut -------------------------------------------------
    filters.append(popup_filter)

    # --- framed popup: scale, rounded-corner alpha mask, fade in/out ----------
    r = corner_radius
    # Fully opaque except inside the four corner circles (radius r).
    alpha_expr = (
        f"if(gt(abs(X-(W/2)),(W/2-{r}))*gt(abs(Y-(H/2)),(H/2-{r})),"
        f"if(lte(pow(abs(X-(W/2))-(W/2-{r}),2)+pow(abs(Y-(H/2))-(H/2-{r}),2),pow({r},2)),255,0),"
        "255)"
    )
    framed = (
        f"[popup_cut]scale=trunc(iw*{scale:.6f}/2)*2:trunc(ih*{scale:.6f}/2)*2,"
        "format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'"
    )
    if open_eff > 0:
        framed += f",fade=t=in:alpha=1:d={open_eff:.6f}"
    if not hold_open_at_end and close_eff > 0:
        framed += f",fade=t=out:alpha=1:st={span - close_eff:.6f}:d={close_eff:.6f}"

    # --- overlay onto the dimmed backdrop (backdrop pins the length) ----------
    if shadow:
        framed += ",split=2[framed1][framed2]"
        filters.append(framed)
        # Drop shadow: the popup's (faded) alpha, painted black and blurred, so
        # it fades in step with the window and softly extends past its edges.
        filters.append("[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]")
        filters.append("[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]")
        overlay_base = "[with_shadow][framed1]"
    else:
        framed += "[framed1]"
        filters.append(framed)
        overlay_base = "[dim][framed1]"
    filters.append(
        f"{overlay_base}overlay=x=(W-w)/2:y=(H-h)/2,"
        "settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]"
    )

    # --- tail (verbatim main) -------------------------------------------------
    if has_tail:
        filters.append(
            f"[main_tail_src]trim=start={closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )

    # --- concat pre? + mid + tail? -------------------------------------------
    labels: list[str] = []
    if has_pre:
        labels.append("[main_pre]")
    labels.append("[mid]")
    if has_tail:
        labels.append("[main_tail]")
    if len(labels) == 1:
        filters.append("[mid]null[outv]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

    _run_to_output(
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

    # Fail loud if the CFR backdrop still came out empty: the composite would be
    # short by ~the popup span and later trip the audio-bed duration guards.
    produced = probe_duration(out)
    if produced + 0.2 < main_duration:
        raise ValueError(
            f"floating composite duration ({produced:.3f}s) is short of main "
            f"({main_duration:.3f}s); the CFR backdrop came out empty"
        )


def _compose_slide(
    *,
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    main_duration: float,
    popup_span: float,
    popup_filter: str,
    has_pre: bool,
    has_tail: bool,
    slide_ms: int,
    hold_open_at_end: bool,
) -> None:
    """Assemble and run the sliding-popup composite filtergraph.

    Same skeleton as ``_compose_floating``: the main input is CFR-normalised
    (``fps``) *before* a 3-way split so the always-consumed middle segment
    (``main[opened_at:closed_at]``) fills the whole span even when the
    backgrounded main page emitted no frames there. The mid is two overlays over
    a CFR colour base (VFR-safe timing, ``eof_action=pass`` repeats the last
    frame): the main pushes out to the left while the full-size popup pushes in
    from the right, tiling exactly (both driven by the same ``prog`` expression,
    so there is never a black seam). ``pre``/``tail`` are verbatim main. Concat
    ``pre? + mid + tail?`` (mid always in). The post-encode duration fail-loud
    guard transfers unchanged.
    """

    span = popup_span
    # D_in/D_out clamp to the interval so a short span cannot overrun; the
    # ``<= 0`` guard mirrors float's ``open_ms=0`` guard so ``prog`` never forms
    # a ``t/0`` (which would be inf/NaN and warp the push geometry).
    d_in = min(slide_ms / 1000.0, span / 2.0)
    d_out = min(slide_ms / 1000.0, span - d_in)
    rate = _probe_fps(main)
    width, height = _probe_size(main)

    # prog: 0->1 push-in over D_in, hold at 1, then 1->0 push-out over D_out.
    # A collapsed phase (D<=0) becomes the constant "1" (no division). With
    # hold_open_at_end the whole push-out term is dropped so the popup holds.
    rise = "1" if d_in <= 0 else f"min(1,t/{d_in:.6f})"
    if hold_open_at_end or d_out <= 0:
        fall = "1"
    else:
        fall = f"max(0,min(1,({span:.6f}-t)/{d_out:.6f}))"
    prog = rise if fall == "1" else f"min({rise},{fall})"

    filters: list[str] = []

    # --- CFR normalise, then 3-way split (mid is ALWAYS consumed) -------------
    split_targets: list[str] = []
    if has_pre:
        split_targets.append("[main_pre_src]")
    split_targets.append("[main_mid_src]")
    if has_tail:
        split_targets.append("[main_tail_src]")
    main_norm = f"[0:v]fps={rate:.6f},settb=AVTB,setpts=PTS-STARTPTS"
    if len(split_targets) == 1:
        filters.append(f"{main_norm}[main_mid_src]")
    else:
        filters.append(f"{main_norm},split={len(split_targets)}{''.join(split_targets)}")

    # --- pre (verbatim main) --------------------------------------------------
    if has_pre:
        filters.append(
            f"[main_pre_src]trim=start=0:end={opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )

    # --- mid_main = main[opened:closed] (full-size, NOT scaled) ---------------
    filters.append(
        f"[main_mid_src]trim=start={opened_at:.6f}:end={closed_at:.6f},"
        "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]"
    )

    # --- the reused popup cut (verbatim, full-size) ---------------------------
    filters.append(popup_filter)

    # --- CFR colour base pins output timing (VFR-safe) ------------------------
    filters.append(
        f"color=black:size={width}x{height}:rate={rate:.6f}:duration={span:.6f},"
        "settb=AVTB,setpts=PTS-STARTPTS[base]"
    )

    # --- two overlays: main exits left, popup enters right (same prog) --------
    # ``overlay``'s ``W`` is the base width; the two layers cover [-W*prog,
    # W-W*prog) and [W-W*prog, ...) with the same expression/rounding, so they
    # tile exactly (probe-confirmed: no black seam).
    filters.append(
        f"[base][mid_main]overlay=x='-W*({prog})':y=0:eof_action=pass[wmain]"
    )
    filters.append(
        f"[wmain][popup_cut]overlay=x='W*(1-({prog}))':y=0:eof_action=pass,"
        "settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]"
    )

    # --- tail (verbatim main) -------------------------------------------------
    if has_tail:
        filters.append(
            f"[main_tail_src]trim=start={closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )

    # --- concat pre? + mid + tail? -------------------------------------------
    labels: list[str] = []
    if has_pre:
        labels.append("[main_pre]")
    labels.append("[mid]")
    if has_tail:
        labels.append("[main_tail]")
    if len(labels) == 1:
        filters.append("[mid]null[outv]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

    _run_to_output(
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

    # Fail loud if the CFR base still came out empty: the composite would be short
    # by ~the popup span and later trip the audio-bed duration guards.
    produced = probe_duration(out)
    if produced + 0.2 < main_duration:
        raise ValueError(
            f"slide composite duration ({produced:.3f}s) is short of main "
            f"({main_duration:.3f}s); the CFR base came out empty"
        )


def mux(video: Path, audio: Path, out: Path) -> None:
    """Combine *video* and *audio* into *out*.

    Video is transcoded to H.264 (Playwright records VP8/WebM, which the MP4
    container does not accept — a stream copy would fail); audio is encoded to
    AAC at the canonical 48000 Hz sample rate. ``-shortest`` clips output to the
    shorter of the two streams so the audio bed never runs past the recording.
    """
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    _run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            str(SAMPLE_RATE),
            "-shortest",
        ],
        out,
    )


def mux_audio_tracks(
    video: Path,
    tracks: list[MuxAudioTrack],
    out: Path,
    *,
    preencoded: bool = False,
) -> None:
    """Attach one or more language-tagged audio tracks to a single MP4 video.

    The first track must be the sole default stream. Every audio bed must already
    match the video duration; the video clock is authoritative and ``-shortest``
    is deliberately avoided so a malformed short track cannot truncate the film.
    ``preencoded`` copies an already H.264-compatible picture (the popup
    compositor path); otherwise Playwright's WebM picture is encoded to H.264.
    """

    video, out = Path(video), Path(out)
    tracks = [
        MuxAudioTrack(
            path=Path(track.path),
            language=track.language,
            title=track.title,
            default=track.default,
        )
        for track in tracks
    ]
    if not tracks:
        raise ValueError("at least one audio track is required")
    default_indices = [index for index, track in enumerate(tracks) if track.default]
    if default_indices != [0]:
        raise ValueError("exactly one default audio track is required and it must be first")
    languages = [track.language for track in tracks]
    if any(not is_iso_639_2(language) for language in languages):
        raise ValueError("audio track language must be a registered ISO 639-2 code")
    if len(languages) != len(set(languages)):
        raise ValueError("audio track languages must be unique")

    _check_sources(video, *(track.path for track in tracks))
    video_duration = probe_duration(video)
    duration_tolerance = 0.05
    for track in tracks:
        audio_duration = probe_duration(track.path)
        if abs(audio_duration - video_duration) > duration_tolerance:
            raise ValueError(
                f"audio track {track.language} duration ({audio_duration}) does not match "
                f"video duration ({video_duration})"
            )

    cmd = [ffmpeg_bin(), "-y", "-i", str(video)]
    for track in tracks:
        cmd += ["-i", str(track.path)]
    cmd += ["-map", "0:v:0"]
    for input_index in range(1, len(tracks) + 1):
        cmd += ["-map", f"{input_index}:a:0"]
    if preencoded:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += [
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-b:a",
        "192k",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    for stream_index, track in enumerate(tracks):
        title = track.title or track.language
        cmd += [
            f"-metadata:s:a:{stream_index}",
            f"language={track.language}",
            f"-metadata:s:a:{stream_index}",
            f"title={title}",
            f"-metadata:s:a:{stream_index}",
            f"handler_name={title}",
            f"-disposition:a:{stream_index}",
            "default" if track.default else "0",
        ]
    cmd += [
        "-movflags",
        "+faststart",
        "-t",
        f"{video_duration:.6f}",
    ]
    _run_to_output(cmd, out)


def mux_preencoded(video: Path, audio: Path, out: Path) -> None:
    """Attach audio to an MP4-compatible video without re-encoding its picture."""
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    _run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            str(SAMPLE_RATE),
            "-shortest",
        ],
        out,
    )
