"""ffmpeg-backed tests for ``video.mux.probe`` — duration probing and ``_probe_all``.

Input material is generated with ffmpeg's ``testsrc`` lavfi source, so the tests
need no fixtures on disk; they are skipped when ffmpeg/ffprobe are not installed.
No shared conftest by design — the shared builders and the marker block come from
the explicitly imported ``_mux_helpers`` (see that module's docstring for why).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from guidebot_recorder.video.mux.probe import probe_duration
from tests.unit.video._mux_helpers import FFMPEG, _make_video

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


def test_probe_duration_matches(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    _make_video(video, 2.0)
    assert probe_duration(video) == pytest.approx(2.0, abs=0.3)


def test_probe_duration_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_duration(tmp_path / "nope.mp4")


def test_probe_all_is_fresh_after_input_is_rewritten(tmp_path: Path) -> None:
    video = tmp_path / "rewritten.mp4"
    _make_video(video, 1.0)

    first = mux_module._probe_all(video)

    _make_video(video, 2.0)
    second = mux_module._probe_all(video)

    assert first.duration == pytest.approx(1.0, abs=0.3)
    assert second.duration == pytest.approx(2.0, abs=0.3)
