"""ffmpeg-backed tests for ``video.mux.slide`` — the slide (push in/hold/out) picture.

Slide mode pushes the popup in over the main page, holds it full-frame, then pushes
it out. These sample either side of the moving boundary to prove both layers coexist
mid-push, and cover the CFR frame count, the no-black-flash guard at push-out end,
tail clock alignment, hold-open, the degenerate zero/short spans, and that each
artifact is probed once; the exact graphs are pinned in ``test_mux_filtergraph.py``.

Input material is generated with ffmpeg's lavfi sources, so the tests need no
fixtures on disk; they are skipped when ffmpeg/ffprobe are not installed. No
shared conftest by design — the shared builders, samplers and the marker block
come from the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import compose_popup_video
from guidebot_recorder.video.mux.probe import probe_duration
from tests.unit.video._mux_helpers import (
    _BORDER,
    _CENTER,
    FFMPEG,
    _assert_rgb,
    _assert_yellow,
    _make_color_video,
    _make_main_color_timeline,
    _sample_region_rgb,
    _video_codec,
)

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


def _frame_count(path: Path) -> int:
    """Return the decoded video frame count of *path* (via ffprobe)."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip())


# Two strips either side of the sliding boundary during a push (320px wide frame):
# a left strip (main, still on screen) and a right strip (popup, entering).
_LEFT_STRIP = "40:40:40:100"
_RIGHT_STRIP = "40:40:240:100"


def test_compose_popup_video_slide_pushes_in_holds_and_out(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)  # red 0-1s, green 1-2s, blue 2-3s
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=200
    )

    # Full-length film, one H.264 encode, CFR frame count.
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    assert _frame_count(out) == pytest.approx(round(25 * 3.0), abs=3)
    # Pre/tail are the verbatim main page.
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    # During the push-in a single frame shows BOTH layers: green main still on the
    # left, yellow popup entering on the right (a moving boundary, not a cut).
    _assert_rgb(_sample_region_rgb(out, 1.1, _LEFT_STRIP), (0, 255, 0))
    _assert_yellow(_sample_region_rgb(out, 1.1, _RIGHT_STRIP))
    # Mid hold is FULL-FRAME popup: centre AND border are both popup yellow
    # (unlike float, where the border stays dimmed main).
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_slide_composition_probes_each_artifact_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "out.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)
    probed_paths: list[Path] = []
    original_run = mux_module.ffmpeg._run

    def recording_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if Path(cmd[0]).name == "ffprobe":
            probed_paths.append(Path(cmd[-1]))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(mux_module.ffmpeg, "_run", recording_run)

    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.0,
        transition="slide",
    )

    assert probed_paths.count(main) == 1
    assert probed_paths.count(popup) == 1
    assert probed_paths.count(out) == 1


def test_compose_popup_video_slide_no_black_flash_at_push_out_end(tmp_path: Path) -> None:
    # A non-frame-aligned opened/closed leaves the CFR mid one frame short of the
    # colour base; with eof_action=pass the final mid frame flashed BLACK (the base
    # showing through) right before the tail. eof_action=repeat holds the last main
    # frame instead. Interval sits inside the green segment so the push-out returns
    # to green; assert no near-black frame across the tail of the push-out.
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)  # red 0-1, green 1-2, blue 2-3
    _make_color_video(popup, "yellow", 1.0)

    # These bounds are deliberately NOT frame-aligned (25fps): they leave the CFR
    # mid one frame short of the base, which is what triggered the flash. (Verified:
    # with eof_action=pass the frames at t≈1.97..1.988 render black.)
    compose_popup_video(
        main, popup, out, opened_at=1.01, closed_at=1.99, transition="slide", slide_ms=200
    )

    for offset in (0.02, 0.01, 0.005, 0.002):
        red, green, blue = _sample_region_rgb(out, 1.99 - offset, _CENTER)
        assert not (red < 40 and green < 40 and blue < 40), (
            f"black flash at t={1.99 - offset:.3f}: {(red, green, blue)}"
        )


def test_compose_popup_video_slide_tail_clock_alignment(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=200
    )

    # A frame sampled just after closed_at must equal main's colour at that time:
    # main is blue from 2s, so an offset/time-warp in the tail would show green.
    _assert_rgb(_sample_region_rgb(out, 2.05, None), (0, 0, 255))


def test_compose_popup_video_slide_hold_open_at_end(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    # Popup open to end-of-main: no tail, no push-out; hold full-frame to the end.
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=2.0,
        closed_at=3.0,
        transition="slide",
        slide_ms=200,
        hold_open_at_end=True,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # Pre is verbatim main (red then green).
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 1.5, None), (0, 255, 0))
    # Last frame is full-frame popup (no push-out revealing main): centre + border.
    _assert_yellow(_sample_region_rgb(out, 2.9, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 2.9, _BORDER))


def test_compose_popup_video_slide_no_pre_renders(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Popup opens at t~0: no pre segment, mid + tail only. Still pushes in.
    compose_popup_video(
        main, popup, out, opened_at=0.0, closed_at=1.0, transition="slide", slide_ms=200
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 0.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 0.5, _BORDER))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_slide_zero_ms_renders(tmp_path: Path) -> None:
    # slide_ms=0 (a valid "no slide" config) must not divide by zero (t/0): both
    # D_in and D_out collapse to 0, so prog is constant 1 (full-frame the whole mid).
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=0
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_slide_clamps_short_interval(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.5)

    # span (0.3s) < 2 x slide_ms (0.4s): D_in/D_out must clamp, not overrun.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=1.3, transition="slide", slide_ms=200
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
