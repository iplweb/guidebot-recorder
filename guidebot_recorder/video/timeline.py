"""Render time as data: an explicit, frame-exact map from recording to film.

Playwright records at a fixed 25 fps, so the whole model is expressed in whole
frames. Seconds only appear at the boundaries — wall-clock readings convert in,
audio offsets convert out. Nothing in between is a float, because ``tpad``
quantises to the frame grid and any float slack accumulates across freezes until
it trips the duration guard in :mod:`guidebot_recorder.video.mux`.

Two axes exist:

``t_real``
    the recording produced by Playwright.
``t_virtual``
    the finished film, after freezes are inserted and cuts removed.

:meth:`Timeline.to_virtual` is the only bridge between them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from guidebot_recorder.video.mux import ffmpeg, ffmpeg_bin, ffprobe_bin, probe

FPS = 25
"""Frames per second of a Playwright screencast.

Hardcoded in Playwright's ``videoRecorder`` and not exposed through the API.
Verified: every inter-frame delta is exactly 0.04 s, even when the page's main
thread stalls (Chromium repeats the last frame rather than dropping the grid).
"""

EditKind = Literal["freeze", "cut"]


class TimelineError(ValueError):
    """A timeline that cannot be rendered — raised before ffmpeg is invoked."""


def seconds_to_frames(seconds: float) -> int:
    """Convert *seconds* to the nearest whole frame."""
    return int(round(seconds * FPS))


def frames_to_seconds(frames: int) -> float:
    """Convert a whole number of *frames* to seconds, exactly on the grid."""
    return frames / FPS


@dataclass(frozen=True)
class TimeEdit:
    """One edit applied to the recording axis.

    ``freeze``
        hold the frame at index *at* for *frames* additional frames.
    ``cut``
        drop frames ``[at, at + frames)`` from the recording.
    """

    at: int
    kind: EditKind
    frames: int


@dataclass(frozen=True)
class Timeline:
    """An ordered, validated set of edits over a recording of known length."""

    edits: tuple[TimeEdit, ...]
    source_frames: int

    @classmethod
    def build(cls, edits: Iterable[TimeEdit], source_frames: int) -> Timeline:
        """Validate and order *edits*, failing loud on anything unrenderable."""
        if source_frames <= 0:
            raise TimelineError(f"source_frames must be positive, got {source_frames}")

        ordered = tuple(sorted(edits, key=lambda e: e.at))

        for edit in ordered:
            if edit.at < 0:
                raise TimelineError(f"edit position must be >= 0, got {edit.at}")
            if edit.frames <= 0:
                raise TimelineError(f"edit length must be positive, got {edit.frames}")
            if edit.at >= source_frames:
                raise TimelineError(
                    f"edit at frame {edit.at} is beyond the recording ({source_frames} frames)"
                )
            if edit.kind == "cut" and edit.at + edit.frames > source_frames:
                raise TimelineError(
                    f"cut [{edit.at}, {edit.at + edit.frames}) overruns "
                    f"the recording ({source_frames} frames)"
                )

        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.at == previous.at:
                raise TimelineError(f"two edits share frame {current.at}")
            if previous.kind == "cut" and current.at < previous.at + previous.frames:
                raise TimelineError(
                    f"edit at frame {current.at} falls inside the cut "
                    f"[{previous.at}, {previous.at + previous.frames})"
                )

        return cls(edits=ordered, source_frames=source_frames)

    @property
    def is_empty(self) -> bool:
        """Whether this timeline leaves the recording untouched."""
        return not self.edits

    def to_virtual(self, frame: int) -> int:
        """Map a recording frame index onto the finished film.

        A frame inside a cut span clamps to that span's start: cuts remove dead
        time, so nothing meaningful is lost, but the choice is deliberate rather
        than an accident of arithmetic.
        """
        shift = 0
        for edit in self.edits:
            if edit.kind == "freeze":
                if edit.at < frame:
                    shift += edit.frames
            else:
                if edit.at + edit.frames <= frame:
                    shift -= edit.frames
                elif edit.at < frame:
                    return edit.at + shift
        return frame + shift

    def to_virtual_seconds(self, t_real: float) -> float:
        """Map a wall-clock offset (seconds) onto the finished film."""
        return frames_to_seconds(self.to_virtual(seconds_to_frames(t_real)))

    @property
    def virtual_frames(self) -> int:
        """Length of the finished film, in frames."""
        return self.to_virtual(self.source_frames)

    @property
    def virtual_duration(self) -> float:
        """Length of the finished film, in seconds."""
        return frames_to_seconds(self.virtual_frames)

    def to_json(self) -> str:
        """Serialise both axes for diagnostics."""
        return json.dumps(
            {
                "fps": FPS,
                "source_frames": self.source_frames,
                "virtual_frames": self.virtual_frames,
                "virtual_duration": self.virtual_duration,
                "edits": [{"at": e.at, "kind": e.kind, "frames": e.frames} for e in self.edits],
            },
            indent=2,
        )


def _segments(timeline: Timeline) -> list[tuple[int, int, int]]:
    """Decompose a timeline into ordered output segments.

    Yields ``(start_frame, end_frame, pad)``: source frames ``[start, end)``
    followed by ``pad`` extra copies of frame ``end - 1``. A segment is one
    contiguous run of the recording; it ends at a cut, at the end of the
    recording, or at a freeze — and a freeze is exactly "this run's last frame,
    held ``pad`` frames longer", so it needs no segment of its own.

    Folding the freeze into the run that precedes it is not cosmetic. ``concat``
    infers each input's duration from the frames it sees, so a single-frame
    input measures as zero-length and the next segment lands on the same
    timestamp instead of after it — a duplicate PTS at the concat stage.
    ``-vsync cfr`` resolves a single such collision by dropping the duplicate
    frame, so the final frame count can still come out correct; the risk is in
    letting collisions accumulate, not in any one of them being unrenderable.
    Emitting the freeze separately produced exactly that: a freeze two frames
    after its predecessor left a one-frame ``keep`` between them. Folded, a
    freeze-terminated run is always at least two frames long (one source frame
    plus at least one clone), so the collision cannot arise in the first place.
    """
    out: list[tuple[int, int, int]] = []
    cursor = 0
    for edit in timeline.edits:
        if edit.kind == "freeze":
            out.append((cursor, edit.at + 1, edit.frames))
            cursor = edit.at + 1
        else:
            if edit.at > cursor:
                out.append((cursor, edit.at, 0))
            cursor = edit.at + edit.frames
    if cursor < timeline.source_frames:
        out.append((cursor, timeline.source_frames, 0))
    return out


def build_filtergraph(timeline: Timeline) -> str:
    """Build the ffmpeg ``-filter_complex`` graph realising *timeline*.

    Boundaries are frame indices, never float seconds: ``trim=start_frame`` and
    ``tpad=stop=N`` are exact on the frame grid, whereas their second-valued
    counterparts round to the nearest frame and accumulate error across freezes.

    The leading ``fps=25`` is a no-op on current Playwright output (verified
    frame-for-frame identical) and is kept only as a defensive normaliser.

    No segment but the last may be a single frame: ``concat`` derives an input's
    frame duration from the frames it receives, so a one-frame input measures as
    zero-length and the following segment lands on the same output timestamp
    instead of after it — a duplicate PTS at the concat stage. A single such
    collision is not fatal by itself (``-vsync cfr`` resolves it by dropping the
    duplicate, and the film's final frame count and content can still come out
    correct), but nothing bounds how many collisions could accumulate, and each
    one silently retimes everything after it until the encoder papers over it.
    :func:`_segments` cannot produce such a segment from freezes; only a lone
    kept frame butting up against a cut can, and rather than rely on the
    encoder to keep recovering, this is rejected here as a defence-in-depth
    guard against that condition ever reaching ``concat`` at all.
    """
    if timeline.is_empty:
        raise TimelineError("cannot build a filtergraph for an empty timeline")

    segments = _segments(timeline)
    count = len(segments)
    splits = "".join(f"[s{i}]" for i in range(count))
    parts = [f"[0:v]fps={FPS},split={count}{splits}"]

    for i, (start, end, pad) in enumerate(segments):
        if end - start + pad < 2 and i < count - 1:
            raise TimelineError(
                f"segment [{start}, {end}) would emit a single frame ahead of "
                f"{count - i - 1} more; concat cannot measure a one-frame input"
            )
        # The final segment omits end_frame so it runs to the end of input.
        bounds = (
            f"trim=start_frame={start}:end_frame={end}"
            if end < timeline.source_frames
            else f"trim=start_frame={start}"
        )
        chain = f"[s{i}]{bounds},setpts=PTS-STARTPTS"
        if pad:
            chain += f",tpad=stop_mode=clone:stop={pad}"
        parts.append(f"{chain}[v{i}]")

    labels = "".join(f"[v{i}]" for i in range(count))
    parts.append(f"{labels}concat=n={count}:v=1:a=0[v]")
    return ";".join(parts)


def probe_frame_count(path: Path) -> int:
    """Return the number of video frames in *path*, on the 25 fps grid.

    WebM reports ``nb_frames`` as ``N/A``, so the count is derived from the
    container duration. Fails loud when the duration is not a clean multiple of
    the frame interval, which would mean the input is not the CFR material the
    whole time model assumes.
    """
    duration = probe.probe_duration(path)
    exact = duration * FPS
    frames = int(round(exact))
    if abs(exact - frames) > 0.1:
        raise TimelineError(
            f"{path} is {duration}s, which is not a whole number of {FPS}fps frames "
            f"({exact:.3f}) — the recording is not on the expected frame grid"
        )
    return frames


def assert_recording_fps(path: Path) -> None:
    """Fail loud unless *path*'s video stream is exactly ``25/1``.

    We assert rather than adapt: 25 is a hardcoded Playwright constant, so a
    different value means the recorder changed under us. Silently re-quantising
    the audio timeline onto a new grid would turn that into a subtle desync.
    """
    proc = ffmpeg._run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
    )
    rate = proc.stdout.strip()
    if rate != f"{FPS}/1":
        raise TimelineError(
            f"{path} reports {rate!r} fps, expected {FPS}/1 — "
            "the frame-exact time model does not hold for this input"
        )


def apply_time_edits(src: Path, timeline: Timeline, out: Path) -> None:
    """Rewrite *src* into *out* with *timeline*'s freezes and cuts applied."""
    src = Path(src)
    out = Path(out)
    if not src.exists():
        raise FileNotFoundError(src)
    if timeline.is_empty:
        raise TimelineError("apply_time_edits called with an empty timeline")

    actual = probe_frame_count(src)
    if actual != timeline.source_frames:
        raise TimelineError(
            f"timeline was built for {timeline.source_frames} frames but {src} has {actual}"
        )

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-filter_complex",
        build_filtergraph(timeline),
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-vsync",
        "cfr",
        "-movflags",
        "+faststart",
    ]
    ffmpeg._run_to_output(cmd, out)
