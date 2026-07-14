"""Build a silence-padded audio bed from placed narration segments.

Each narration segment is delayed to its start offset (``adelay``) and mixed
(``amix``) over a silent base of the requested total duration. The output is
trimmed/padded to exactly *total* seconds at the canonical 48000 Hz sample rate.

The segment type is intentionally structural (duck-typed): any object exposing
``.path`` (a :class:`~pathlib.Path`) and ``.duration`` (seconds, unused here but
part of the contract) works. This keeps ``video/`` independent of ``tts/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from guidebot_recorder.video.mux import SAMPLE_RATE, _run, ffmpeg_bin


@runtime_checkable
class SegmentLike(Protocol):
    """Structural type for a synthesized narration segment."""

    path: Path
    duration: float


@dataclass
class Placed:
    """A narration *segment* scheduled to start at *offset* seconds."""

    segment: SegmentLike
    offset: float


def build_audio_bed(placed: list[Placed], total: float, out: Path) -> None:
    """Render the mixed narration bed to *out*, exactly *total* seconds long.

    Segments are delayed to their offsets and summed over a silent base spanning
    the whole timeline, so gaps between narration become silence and the result
    is padded (or trimmed) to *total*.
    """
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    for p in placed:
        if p.offset < 0:
            raise ValueError(f"segment offset must be >= 0, got {p.offset}")
        if not Path(p.segment.path).exists():
            raise FileNotFoundError(p.segment.path)

    # Input 0 is a silent base of the full timeline; real segments follow.
    cmd: list[str] = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{total:.6f}",
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
    ]
    for p in placed:
        cmd += ["-i", str(p.segment.path)]

    # Delay each real segment to its offset, normalized to 48000/stereo so the
    # delay and the mix are unambiguous regardless of the source layout.
    filters: list[str] = []
    mix_labels = ["[0:a]"]  # the silent base
    for i, p in enumerate(placed, start=1):
        delay_ms = int(round(p.offset * 1000))
        label = f"d{i}"
        filters.append(
            f"[{i}:a]aresample={SAMPLE_RATE},"
            f"aformat=channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        mix_labels.append(f"[{label}]")

    mix_inputs = len(placed) + 1
    filters.append(
        f"{''.join(mix_labels)}amix=inputs={mix_inputs}:duration=longest:normalize=0[out]"
    )
    filter_complex = ";".join(filters)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        str(SAMPLE_RATE),
        # Enforce the exact timeline length (trim overruns, keep silent padding).
        "-t",
        f"{total:.6f}",
        str(out),
    ]
    _run(cmd)
