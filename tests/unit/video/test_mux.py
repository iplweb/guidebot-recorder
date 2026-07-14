"""ffmpeg-backed tests for probe_duration + mux (marked ``ffmpeg``).

Input material is generated with ffmpeg's ``testsrc``/``sine`` lavfi sources, so
the tests need no fixtures on disk. They are skipped when ffmpeg/ffprobe are not
installed (no shared conftest by design).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import mux, probe_duration

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]


def _make_video(path: Path, seconds: float) -> None:
    """Write an H.264 mp4 (video only) of *seconds* duration."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_audio(path: Path, seconds: float) -> None:
    """Write a mono WAV tone of *seconds* duration."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000",
            "-t", str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _stream_types(path: Path) -> list[str]:
    """Return the codec_type of each stream in *path* (via ffprobe)."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()


def test_probe_duration_matches(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    _make_video(video, 2.0)
    assert probe_duration(video) == pytest.approx(2.0, abs=0.3)


def test_probe_duration_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_duration(tmp_path / "nope.mp4")


def test_mux_produces_one_video_one_audio(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 2.0)
    _make_audio(audio, 2.0)

    mux(video, audio, out)

    assert out.exists()
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1
    assert len(types) == 2
    # -shortest keeps the muxed file close to the (equal) input lengths.
    assert probe_duration(out) == pytest.approx(2.0, abs=0.4)


def test_mux_shortest_clips_to_shorter_stream(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 3.0)
    _make_audio(audio, 1.0)

    mux(video, audio, out)

    # -shortest → output no longer than the 1s audio.
    assert probe_duration(out) == pytest.approx(1.0, abs=0.5)
