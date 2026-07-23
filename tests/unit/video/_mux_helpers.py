"""Shared fixtures-by-hand for the ``video.mux`` tests: inputs, markers, the spy.

Not a ``conftest.py`` on purpose (design decision D4): every test file imports
what it uses by name, so reading one file still shows where its helpers come from.

Everything here is generated with ffmpeg's ``lavfi`` sources, so the mux tests need
no fixtures on disk — and every consumer needs the same skip marker, which is why
:data:`FFMPEG` is exported rather than repeated. ``pytestmark`` is *not* inherited
through an import, so each test module must assign it itself.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

mux_module = importlib.import_module("guidebot_recorder.video.mux")

#: The marker block every ffmpeg-backed mux test module needs. Assign it, do not
#: import-and-forget: a lost ``skipif`` changes what CI runs and says nothing.
FFMPEG = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]


def _make_audio(path: Path, seconds: float) -> None:
    """Write a mono WAV tone of *seconds* duration."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate=48000",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_color_video(path: Path, color: str, seconds: float) -> None:
    """Write a solid-colour H.264 video."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:duration={seconds}:size=320x240:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_popup_with_filler(path: Path, seconds: float) -> None:
    """Write a 320x240 popup whose real window is only the top-left 160x120.

    Mimics a popup recorded onto the main window's canvas: yellow content in the
    top-left corner, grey filler everywhere else.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x808080:duration={seconds}:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=160x120:rate=25",
            "-filter_complex",
            "[0:v][1:v]overlay=x=0:y=0,format=yuv420p[outv]",
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_popup_with_teardown_tail(
    path: Path,
    *,
    good_seconds: float = 0.8,
    tail_seconds: float = 0.2,
    window: tuple[int, int] = (200, 150),
    shrunk: tuple[int, int] | None = (150, 110),
) -> None:
    """Write a popup recording whose window shrinks for its final frames.

    Mimics a headed render's teardown: the page content is unchanged but Chromium
    stops rasterising the window at the screen's backing scale, so Playwright's
    mid-grey padding grows and a crop sized from the stable part starts exposing
    filler. ``shrunk=None`` writes a recording that never shrinks.
    """
    inputs: list[str] = []
    parts: list[str] = []
    sizes = [(window, good_seconds)]
    if shrunk is not None:
        sizes.append((shrunk, tail_seconds))
    for index, ((width, height), seconds) in enumerate(sizes):
        inputs += [
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:duration={seconds}:size={width}x{height}:rate=25",
        ]
        # 0x808080 is the mid-grey Chromium pads a popup's canvas with.
        parts.append(f"[{index}:v]pad=320:240:0:0:0x808080[p{index}]")
    concat = "".join(f"[p{i}]" for i in range(len(sizes)))
    parts.append(f"{concat}concat=n={len(sizes)}:v=1:a=0,format=yuv420p[outv]")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_video(path: Path, seconds: float) -> None:
    """Write an H.264 mp4 (video only) of *seconds* duration from ``testsrc``."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_main_color_timeline(path: Path) -> None:
    """Write red (0-1s), green (1-2s), then blue (2-3s)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:duration=1:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x00ff00:duration=1:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:duration=1:size=320x240:rate=25",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[outv]",
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


# Regions of the 320x240 frame: the popup centre and a left border strip that
# floating mode leaves as (dimmed) backdrop at scale=0.72.
_CENTER = "40:40:140:100"
_BORDER = "10:240:0:0"


def _sample_rgb(path: Path, at: float) -> tuple[int, int, int]:
    """Decode one frame and return its average RGB colour."""
    return _sample_region_rgb(path, at, None)


def _sample_region_rgb(path: Path, at: float, crop: str | None) -> tuple[int, int, int]:
    """Decode one frame (optionally cropped to *crop*) and average it to one RGB.

    *crop* is an ffmpeg ``crop`` spec ``w:h:x:y`` selecting a region before the
    1x1 area downscale, so callers can probe the composite's centre (popup) vs.
    its border (dimmed main) independently.
    """
    vf = "scale=1:1:flags=area" if crop is None else f"crop={crop},scale=1:1:flags=area"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            str(at),
            "-frames:v",
            "1",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    assert len(proc.stdout) == 3
    return tuple(proc.stdout)


def _assert_rgb(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
    assert actual == pytest.approx(expected, abs=20)


def _assert_dimmed_green(rgb: tuple[int, int, int]) -> None:
    """Assert *rgb* reads as backdrop green darkened by the dim ramp."""
    red, green, blue = rgb
    assert green > 50, f"backdrop should still be visibly green: {rgb}"
    assert green < 210, f"backdrop should be dimmed, not full green: {rgb}"
    assert red < 70 and blue < 70, f"backdrop should be green-dominant: {rgb}"


def _assert_yellow(rgb: tuple[int, int, int]) -> None:
    red, green, blue = rgb
    assert red > 170 and green > 170 and blue < 90, f"expected popup yellow: {rgb}"


def _video_codec(path: Path) -> str:
    """Return the ``codec_name`` of the first video stream (via ffprobe)."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def capture_ffmpeg_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of every ffmpeg run, then run it for real.

    Running it too keeps the assertions honest: a command that matches the
    expected argv — or a filtergraph that matches the expected string — but that
    ffmpeg rejects still fails the test.

    Patched on the ``ffmpeg`` submodule, not the facade: the consumers read that
    module's globals at call time (see ``tests/unit/video/test_mux_seams.py``).
    """
    seen: list[list[str]] = []
    real_run = mux_module.ffmpeg._run_to_output

    def spy_run(cmd: list[str], out: Path) -> None:
        seen.append(list(cmd))
        real_run(cmd, out)

    monkeypatch.setattr(mux_module.ffmpeg, "_run_to_output", spy_run)
    return seen


def filtergraph_of(cmd: list[str]) -> str:
    """The ``-filter_complex`` argument of one recorded ffmpeg argv."""
    return cmd[cmd.index("-filter_complex") + 1]
