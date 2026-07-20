"""Real ffmpeg round-trips for the time-edit stage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import ffmpeg_bin
from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    TimelineError,
    apply_time_edits,
    assert_recording_fps,
    probe_frame_count,
)

pytestmark = pytest.mark.ffmpeg

SOURCE_FRAMES = 148


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A 148-frame CFR-25 clip, standing in for a Playwright screencast."""
    out = tmp_path / "src.mp4"
    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=25:duration={SOURCE_FRAMES / 25}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "25",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def test_probe_frame_count_matches_the_source(source: Path) -> None:
    assert probe_frame_count(source) == SOURCE_FRAMES


def test_assert_recording_fps_accepts_twenty_five(source: Path) -> None:
    assert_recording_fps(source)  # does not raise


def test_single_freeze_is_frame_exact(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=SOURCE_FRAMES)
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES + 59


def test_three_freezes_do_not_accumulate_error(source: Path, tmp_path: Path) -> None:
    """The drift regression: float seconds would lose ~30ms here."""
    tl = Timeline.build(
        [TimeEdit(at=25 * (i + 1), kind="freeze", frames=59) for i in range(3)],
        source_frames=SOURCE_FRAMES,
    )
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES + 3 * 59


def test_cut_and_freeze_combined_is_frame_exact(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build(
        [
            TimeEdit(at=25, kind="cut", frames=25),
            TimeEdit(at=75, kind="freeze", frames=59),
        ],
        source_frames=SOURCE_FRAMES,
    )
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES - 25 + 59


def test_output_stays_cfr_twenty_five(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=SOURCE_FRAMES)
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert_recording_fps(out)  # does not raise


def test_apply_rejects_a_timeline_longer_than_the_source(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=999)
    with pytest.raises(TimelineError):
        apply_time_edits(source, tl, tmp_path / "out.mp4")
