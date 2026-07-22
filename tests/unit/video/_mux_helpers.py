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
