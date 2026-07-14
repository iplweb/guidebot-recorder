"""ffprobe duration + ffmpeg mux (video copy + AAC 48000 audio, ``-shortest``).

All helpers are fail-loud: a missing binary or a non-zero exit raises immediately
(no silent fallbacks, per the design's fail-loud rule).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

#: Audio sample rate used everywhere in the montage pipeline (design §8).
SAMPLE_RATE = 48000


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


def mux(video: Path, audio: Path, out: Path) -> None:
    """Combine *video* and *audio* into *out*.

    Video is transcoded to H.264 (Playwright records VP8/WebM, which the MP4
    container does not accept — a stream copy would fail); audio is encoded to
    AAC at the canonical 48000 Hz sample rate. ``-shortest`` clips output to the
    shorter of the two streams so the audio bed never runs past the recording.
    """
    video, audio, out = Path(video), Path(audio), Path(out)
    for src in (video, audio):
        if not src.exists():
            raise FileNotFoundError(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
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
            str(out),
        ]
    )
