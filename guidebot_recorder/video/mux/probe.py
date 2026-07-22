"""ffprobe metadata reads, plus the pre-flight check for missing inputs.

Kept apart from the composition modules so the "one ffprobe per artifact per
composition" policy — and the deliberate absence of any cache, argued on
:func:`_probe_all` — has a single home that every consumer shares.

:func:`probe_duration` is a test **seam**: tests replace it on this module object,
so consumers inside the package must call it through the module (``from . import
probe`` then ``probe.probe_duration(...)``), never by name. See
:mod:`guidebot_recorder.video.mux.ffmpeg` for the rule and its AST guard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .ffmpeg import ffprobe_bin


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    """Metadata read together by one fresh ffprobe process."""

    duration: float
    fps: float
    size: tuple[int, int] | None


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
    :func:`~guidebot_recorder.video.mux.crop.detect_content_crop`); the fail-loud
    callers leave it unset.
    """

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    proc = ffmpeg._run(
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
