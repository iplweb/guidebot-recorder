"""ffmpeg-backed tests for build_audio_bed (marked ``ffmpeg``).

Uses a lightweight duck-typed segment (matching the ``.path``/``.duration``
contract) so ``video/`` stays independent of ``tts/``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import probe_duration

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]


@dataclass
class _FakeSegment:
    """Structural stand-in for tts.base.Segment (path + duration)."""

    path: Path
    duration: float


def _make_tone(path: Path, seconds: float, freq: int = 440) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}:sample_rate=48000",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _sample_rate(path: Path) -> int:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip())


def test_bed_is_padded_to_total(tmp_path: Path) -> None:
    seg_a = tmp_path / "a.wav"
    seg_b = tmp_path / "b.wav"
    _make_tone(seg_a, 1.0, freq=440)
    _make_tone(seg_b, 1.0, freq=660)

    placed = [
        Placed(segment=_FakeSegment(path=seg_a, duration=1.0), offset=0.0),
        Placed(segment=_FakeSegment(path=seg_b, duration=1.0), offset=2.0),
    ]
    out = tmp_path / "bed.wav"
    build_audio_bed(placed, total=4.0, out=out)

    assert out.exists()
    assert probe_duration(out) == pytest.approx(4.0, abs=0.2)
    assert _sample_rate(out) == 48000


def test_bed_trims_overrun_to_total(tmp_path: Path) -> None:
    seg = tmp_path / "long.wav"
    _make_tone(seg, 5.0)
    placed = [Placed(segment=_FakeSegment(path=seg, duration=5.0), offset=0.0)]
    out = tmp_path / "bed.wav"
    build_audio_bed(placed, total=2.0, out=out)

    assert probe_duration(out) == pytest.approx(2.0, abs=0.2)


def test_empty_placed_yields_silence_of_total(tmp_path: Path) -> None:
    out = tmp_path / "bed.wav"
    build_audio_bed([], total=3.0, out=out)
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)


def test_negative_total_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_audio_bed([], total=0.0, out=tmp_path / "x.wav")
