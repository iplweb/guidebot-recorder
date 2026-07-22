"""Pixel heuristics that find a popup's real window inside its padded recording.

Playwright records every page of a context at the *context's* video size, so a
popup arrives as its window plus a flat fill. Everything here reads that fill:
:func:`detect_content_crop` guesses the window from it, :func:`_normalise_popup_crop`
makes a caller-supplied rect safe for yuv420p, and :func:`detect_teardown_tail`
finds the trailing frames where the window stopped filling the crop.

These live apart from the composition modules because they break the package's
fail-loud rule on purpose: they are optional refinements, and every failure
degrades to "no crop" / "no tail" rather than aborting a render. Keeping them in
one module keeps that exception visible instead of scattered.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from pathlib import Path

from . import ffmpeg
from .ffmpeg import ffmpeg_bin
from .probe import _probe_all, _ProbeResult

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
    raw = ffmpeg._run(
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
        # Not named `probe`: that is the name of the sibling *module*, and the
        # seam rule says a seam is reached as `probe.probe_duration(...)`. A local
        # shadowing it would turn the first such call here into an AttributeError
        # on a `_ProbeResult`.
        probed = _probe_all(path, timeout=CROPDETECT_TIMEOUT)
        if probed.size is None or probed.duration <= 0:
            return None
        padding = _padding_color(path, probed.duration / 2, probed.size)
        red, green, blue = padding
        rate = max(1e-3, samples / probed.duration)
        proc = ffmpeg._run(
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
    if (winner[0], winner[1]) == probed.size and (winner[2], winner[3]) == (0, 0):
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
    yuv420p (chroma is subsampled 2x2), the same reason the scale in
    :mod:`~guidebot_recorder.video.mux.floating` uses ``trunc(.../2)*2``. The
    origin snaps down too, so the crop never loses a pixel of real content off
    the top-left.
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
    metadata: _ProbeResult | None = None,
) -> float:
    """Seconds of trailing frames whose window no longer fills *crop*.

    A popup's recorded size is not necessarily constant. Chromium can stop
    rasterising the window at the screen's backing scale for the final frames of
    a headed render — the page content is unchanged, but it arrives smaller, so
    Playwright's padding grows and the fixed crop — sized from the *stable* part
    of the recording, see
    :func:`~guidebot_recorder.recorder.render._recording_scale` — starts exposing
    filler along its right and bottom edges. In a held-open composite that reads
    as the popup abruptly shrinking against a grey slab for the last second of
    the film.

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

    *metadata* lets a caller that already measured *path* share the result, keeping
    one composition to one ffprobe per artifact (see
    :func:`~guidebot_recorder.video.mux.probe._probe_all`). It is not called
    ``probe``: that is the name of the sibling *module*, which the seam rule says
    is reached as ``probe.probe_duration(...)``, and a parameter shadowing it
    would make the first such call in this module fail on a ``_ProbeResult``.
    """
    path = Path(path)
    width, height, x, y = crop
    try:
        if metadata is None:
            metadata = _probe_all(path, timeout=CROPDETECT_TIMEOUT)
        if metadata.size is None or metadata.duration <= 0:
            return 0.0
        canvas_width, canvas_height = metadata.size
        # No padding anywhere means no filler to recognise, so nothing to detect.
        if (x, y) == (0, 0) and (width, height) == metadata.size:
            return 0.0
        filler = _padding_color(path, metadata.duration / 2, metadata.size)
        corner_x = min(max(0, x + width - 2), max(0, canvas_width - 2))
        corner_y = min(max(0, y + height - 2), max(0, canvas_height - 2))
        raw = ffmpeg._run(
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
    tail = trailing * metadata.duration / frames
    if tail > TEARDOWN_TAIL_MAX_FRACTION * metadata.duration:
        return 0.0
    return tail
