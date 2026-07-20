"""ffprobe, FFmpeg video assembly, and audio muxing helpers.

All helpers are fail-loud: a missing binary or a non-zero exit raises immediately
(no silent fallbacks, per the design's fail-loud rule).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
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


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    """Metadata read together by one fresh ffprobe process."""

    duration: float
    fps: float
    size: tuple[int, int] | None


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


def _run(
    cmd: list[str],
    *,
    binary: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run *cmd*, capturing output; raise ``RuntimeError`` on failure.

    ``binary`` keeps stdout as ``bytes`` (raw pixel output); stderr is decoded
    either way so failures stay readable. ``timeout`` lets a caller that can live
    without a result cap its wait: ``subprocess.TimeoutExpired`` propagates (the
    child having been killed) and is that caller's to handle.
    """
    proc = subprocess.run(cmd, capture_output=True, text=not binary, check=False, timeout=timeout)
    if proc.returncode != 0:
        stderr = (
            proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        )
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr}")
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
    return _probe_all(path).duration


def _probe_all(
    path: Path,
    default_fps: float = 25.0,
    *,
    timeout: float | None = None,
) -> _ProbeResult:
    """Read duration, average video FPS, and video size in one ffprobe call.

    Results deliberately are not cached across calls: render outputs are written
    atomically and callers may replace a path between probes. Sharing this result
    is therefore limited to one top-level composition operation, during which its
    input files are immutable.

    ``timeout`` is for callers whose whole operation is optional (see
    :func:`detect_content_crop`); the fail-loud callers leave it unset.
    """

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=avg_frame_rate,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout,
    )
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:  # pragma: no cover - defensive
        raise RuntimeError("ffprobe returned invalid JSON metadata") from exc
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        raise RuntimeError("ffprobe returned invalid JSON metadata")

    raw_duration = payload.get("format", {}).get("duration", "")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"ffprobe returned non-numeric duration: {raw_duration!r}") from exc

    streams = payload.get("streams", [])
    stream = streams[0] if isinstance(streams, list) and streams else {}
    if not isinstance(stream, dict):  # pragma: no cover - defensive
        stream = {}

    raw_fps = stream.get("avg_frame_rate", "")
    try:
        num, _, den = str(raw_fps).partition("/")
        fps = float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        fps = default_fps
    if fps <= 0:
        fps = default_fps

    try:
        size = (int(stream["width"]), int(stream["height"]))
    except (KeyError, TypeError, ValueError):
        size = None
    return _ProbeResult(duration=duration, fps=fps, size=size)


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
    return _probe_all(path, default_fps=default).fps


def _probe_size(path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of *path*'s first video stream in pixels.

    The slide compositor needs concrete dimensions for the CFR ``color`` base the
    two overlays tile across (``overlay``'s ``W`` variable then references this
    base width in the push expressions). Fail loud if ffprobe cannot report them.
    """
    size = _probe_all(path).size
    if size is None:  # pragma: no cover - defensive
        raise RuntimeError("ffprobe returned non-numeric size")
    return size


#: How many frames ``detect_content_crop`` samples across the popup recording.
#: One frame is not enough: animated content (a caret, a spinner) makes a single
#: frame's rect wobble, and a wobbling crop would make the framed window jump.
CROPDETECT_SAMPLES = 12

#: Share of the sampled frames that must agree on the same rect before it is
#: trusted. Below this the recording has no stable window and no crop is emitted.
CROPDETECT_MIN_AGREEMENT = 0.6

#: 8-bit distance from the padding colour above which a pixel counts as content.
#: Small, because the padding is a flat fill: only codec noise sits below it.
CROPDETECT_LIMIT = 8

#: Wall-clock ceiling on each ffmpeg pass of the detection. Generous — the pass
#: decodes the whole popup recording — but finite: the crop is optional and a
#: render must never hang waiting for an optional refinement.
CROPDETECT_TIMEOUT = 60.0

_CROP_LINE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def _padding_color(
    path: Path,
    at_seconds: float,
    size: tuple[int, int],
) -> tuple[int, int, int]:
    """Sample the bottom-right pixel of *path* — the popup canvas's padding.

    Playwright pads a popup's recording out to the *context* video size with a
    flat fill (mid-grey in Chromium). The bottom-right pixel is inside that fill
    for any popup smaller than the canvas; for a popup that fills the canvas it
    is ordinary content, and the detection below then finds nothing to trim,
    which is the correct answer.
    """
    width, height = size
    raw = _run(
        [
            ffmpeg_bin(),
            "-v",
            "error",
            "-ss",
            f"{at_seconds:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            # 2x2, not a single pixel: yuv420p subsamples chroma 2x2 and a crop
            # of width 1 collapses the chroma planes to zero width.
            f"crop=2:2:{max(0, width - 2)}:{max(0, height - 2)},scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        binary=True,
        timeout=CROPDETECT_TIMEOUT,
    ).stdout
    if len(raw) < 3:
        raise RuntimeError(f"could not sample the popup padding colour from {path}")
    return raw[0], raw[1], raw[2]


def detect_content_crop(
    path: Path,
    *,
    samples: int = CROPDETECT_SAMPLES,
    min_agreement: float = CROPDETECT_MIN_AGREEMENT,
) -> tuple[int, int, int, int] | None:
    """Guess a popup recording's real window from its pixels, or ``None``.

    Last resort behind the deterministic geometry (``window.open`` features, the
    popup's content bounding box): the padding Playwright fills the oversized
    canvas with is a flat colour, so mapping every pixel to its distance from
    that colour turns the padding black and lets ``cropdetect`` trim it. Plain
    ``cropdetect`` cannot: it only trims *dark* borders and the padding is
    mid-grey.

    ``samples`` frames spread across the recording are detected independently
    (``reset_count=1``) and the rect must be shared by ``min_agreement`` of them,
    so animated content cannot make the crop jitter frame to frame.

    Unlike the rest of this module this helper is deliberately **not** fail-loud:
    it is a heuristic, and every failure — no ffmpeg, an unreadable file, no
    consensus, a rect covering the whole canvas, an ffmpeg pass overrunning
    :data:`CROPDETECT_TIMEOUT` — degrades to ``None``, i.e. the uncropped
    composite that was the behaviour before it existed.
    """
    path = Path(path)
    try:
        probe = _probe_all(path, timeout=CROPDETECT_TIMEOUT)
        if probe.size is None or probe.duration <= 0:
            return None
        padding = _padding_color(path, probe.duration / 2, probe.size)
        red, green, blue = padding
        rate = max(1e-3, samples / probe.duration)
        proc = _run(
            [
                ffmpeg_bin(),
                "-v",
                "info",
                "-i",
                str(path),
                "-vf",
                (
                    f"fps={rate:.6f},format=gbrp,"
                    f"geq=r='abs(r(X,Y)-{red})':g='abs(g(X,Y)-{green})':b='abs(b(X,Y)-{blue})',"
                    "format=gray,"
                    f"cropdetect=limit={CROPDETECT_LIMIT}:round=2:reset_count=1:skip=0"
                ),
                "-f",
                "null",
                "-",
            ],
            timeout=CROPDETECT_TIMEOUT,
        )
    except (RuntimeError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # Includes the ffmpeg passes overrunning their budget: an optional
        # refinement that costs too much is simply not applied.
        return None

    rects = [
        (int(width), int(height), int(x), int(y))
        for width, height, x, y in _CROP_LINE.findall(proc.stderr)
    ]
    rects = [rect for rect in rects if rect[0] > 0 and rect[1] > 0]
    if not rects:
        return None
    winner, hits = Counter(rects).most_common(1)[0]
    if hits < max(2, math.ceil(min_agreement * len(rects))):
        return None
    if (winner[0], winner[1]) == probe.size and (winner[2], winner[3]) == (0, 0):
        return None  # nothing to trim
    if (winner[2], winner[3]) != (0, 0):
        # Playwright anchors the popup at the canvas's top-left and pads only to
        # the right and below, so genuine padding always leaves the window at the
        # origin. A rect that starts anywhere else is not a window inside padding
        # — it is *ink inside a full-bleed page*, and the "padding" colour sampled
        # at the corner was that page's own background. Cropping to it would cut
        # the popup's background away and frame its text. Decline instead.
        return None
    return winner


def _normalise_popup_crop(
    crop: tuple[int, int, int, int] | None,
    source: tuple[int, int] | None,
) -> tuple[int, int, int, int] | None:
    """Clamp a popup crop rect into *source* and snap it to even pixels.

    *crop* is ``(width, height, x, y)`` in the popup recording's own pixels, as
    reported by the popup page itself. Returns ``None`` when there is nothing to
    crop — no rect given, or a rect that already covers the whole recording — so
    callers emit today's filtergraph verbatim.

    Dimensions snap *down* to even numbers because the composite is encoded as
    yuv420p (chroma is subsampled 2x2), the same reason the scale below uses
    ``trunc(.../2)*2``. The origin snaps down too, so the crop never loses a
    pixel of real content off the top-left.
    """
    if crop is None:
        return None
    width, height, x, y = (int(round(float(value))) for value in crop)
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        raise ValueError(f"popup_crop must be a positive in-frame rect, got {crop}")
    if source is not None:
        source_width, source_height = source
        if x >= source_width or y >= source_height:
            raise ValueError(f"popup_crop origin is outside the popup recording {source}: {crop}")
        width = min(width, source_width - x)
        height = min(height, source_height - y)
    x -= x % 2
    y -= y % 2
    width -= width % 2
    height -= height % 2
    if width <= 0 or height <= 0:
        raise ValueError(f"popup_crop is smaller than one 2x2 chroma block: {crop}")
    if source is not None and (x, y) == (0, 0) and (width, height) == source:
        return None
    return width, height, x, y


#: Largest share of a popup recording that may be written off as teardown tail.
#: The tail observed in practice is one second on recordings tens of seconds
#: long; anything approaching this means the sampled corner is not reporting what
#: :func:`detect_teardown_tail` assumes and the whole measurement is discarded.
TEARDOWN_TAIL_MAX_FRACTION = 0.25


def detect_teardown_tail(
    path: Path,
    crop: tuple[int, int, int, int],
    *,
    probe: _ProbeResult | None = None,
) -> float:
    """Seconds of trailing frames whose window no longer fills *crop*.

    A popup's recorded size is not necessarily constant. Chromium can stop
    rasterising the window at the screen's backing scale for the final frames of
    a headed render — the page content is unchanged, but it arrives smaller, so
    Playwright's padding grows and the fixed crop (sized from the *stable* part
    of the recording, see ``_recording_scale``) starts exposing filler along its
    right and bottom edges. In a held-open composite that reads as the popup
    abruptly shrinking against a grey slab for the last second of the film.

    Detection samples the 2x2 block just inside *crop*'s far corner across every
    frame: while the window fills the crop that block is page content, and the
    moment the window shrinks it becomes filler. The answer is the *trailing run*
    of filler frames — a run that does not reach the last frame is content that
    merely happens to match the filler colour, not a shrunken window.

    Like :func:`detect_content_crop` this is a heuristic and deliberately not
    fail-loud: an unreadable file, a crop that covers the whole canvas (no filler
    to sample), or a run longer than :data:`TEARDOWN_TAIL_MAX_FRACTION` of the
    recording all yield ``0.0`` — trim nothing, which is the behaviour that
    predates this.

    *probe* lets a caller that already measured *path* share the result, keeping
    one composition to one ffprobe per artifact (see :func:`_probe_all`).
    """
    path = Path(path)
    width, height, x, y = crop
    try:
        if probe is None:
            probe = _probe_all(path, timeout=CROPDETECT_TIMEOUT)
        if probe.size is None or probe.duration <= 0:
            return 0.0
        canvas_width, canvas_height = probe.size
        # No padding anywhere means no filler to recognise, so nothing to detect.
        if (x, y) == (0, 0) and (width, height) == probe.size:
            return 0.0
        filler = _padding_color(path, probe.duration / 2, probe.size)
        corner_x = min(max(0, x + width - 2), max(0, canvas_width - 2))
        corner_y = min(max(0, y + height - 2), max(0, canvas_height - 2))
        raw = _run(
            [
                ffmpeg_bin(),
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"crop=2:2:{corner_x}:{corner_y},scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            binary=True,
            timeout=CROPDETECT_TIMEOUT,
        ).stdout
    except (RuntimeError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return 0.0

    frames = len(raw) // 3
    if frames <= 1:
        return 0.0
    trailing = 0
    for index in range(frames - 1, -1, -1):
        pixel = raw[index * 3 : index * 3 + 3]
        if max(abs(pixel[c] - filler[c]) for c in range(3)) > CROPDETECT_LIMIT:
            break
        trailing += 1
    if trailing == 0:
        return 0.0
    tail = trailing * probe.duration / frames
    if tail > TEARDOWN_TAIL_MAX_FRACTION * probe.duration:
        return 0.0
    return tail


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
    # or its length — see `_probe_all` on why results are not cached globally.
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
    rate: float,
    popup_crop: tuple[int, int, int, int] | None = None,
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

    # --- framed popup: crop, scale, rounded-corner alpha mask, fade in/out ----
    # The crop must precede the scale so every downstream cosmetic (the alpha
    # mask's W/H, the fade, the blurred shadow) is computed on the popup's real
    # window rather than on the full-viewport canvas it was recorded onto.
    crop_filter = ""
    if popup_crop is not None:
        crop_width, crop_height, crop_x, crop_y = popup_crop
        crop_filter = f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
    r = corner_radius
    # Fully opaque except inside the four corner circles (radius r).
    alpha_expr = (
        f"if(gt(abs(X-(W/2)),(W/2-{r}))*gt(abs(Y-(H/2)),(H/2-{r})),"
        f"if(lte(pow(abs(X-(W/2))-(W/2-{r}),2)+pow(abs(Y-(H/2))-(H/2-{r}),2),pow({r},2)),255,0),"
        "255)"
    )
    framed = (
        f"[popup_cut]{crop_filter}"
        f"scale=trunc(iw*{scale:.6f}/2)*2:trunc(ih*{scale:.6f}/2)*2,"
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
    rate: float,
    size: tuple[int, int] | None,
) -> None:
    """Assemble and run the sliding-popup composite filtergraph.

    Same skeleton as ``_compose_floating``: the main input is CFR-normalised
    (``fps``) *before* a 3-way split so the always-consumed middle segment
    (``main[opened_at:closed_at]``) fills the whole span even when the
    backgrounded main page emitted no frames there. The mid is two overlays over
    a CFR colour base (VFR-safe timing; ``eof_action=repeat`` holds the last real
    frame if an input is a frame short of the base): the main pushes out to the
    left while the full-size popup pushes in
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
    if size is None:
        raise RuntimeError("ffprobe returned non-numeric size")
    width, height = size

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
    # tile exactly (probe-confirmed: no black seam). ``eof_action=repeat`` (NOT
    # ``pass``, which would show the black base): a fractional ``trim=start=``
    # can leave ``mid_main``/``popup_cut`` one frame short of the CFR base, and
    # ``pass`` would flash black on that final frame (right before the tail);
    # ``repeat`` holds the last real frame while the base pins output length.
    filters.append(f"[base][mid_main]overlay=x='-W*({prog})':y=0:eof_action=repeat[wmain]")
    filters.append(
        f"[wmain][popup_cut]overlay=x='W*(1-({prog}))':y=0:eof_action=repeat,"
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
    video_duration: float | None = None,
) -> None:
    """Attach one or more language-tagged audio tracks to a single MP4 video.

    The first track must be the sole default stream. Every audio bed must already
    match the video duration; the video clock is authoritative and ``-shortest``
    is deliberately avoided so a malformed short track cannot truncate the film.
    ``preencoded`` copies an already H.264-compatible picture (the popup
    compositor path); otherwise Playwright's WebM picture is encoded to H.264.
    Callers that just probed an immutable staged video may pass ``video_duration``
    to avoid launching ffprobe for the same artifact again.
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
    if video_duration is None:
        video_duration = probe_duration(video)
    elif not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError("video_duration must be finite and positive")
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
