"""ffmpeg-backed tests for ``video.mux.floating`` — the float composite picture.

Float mode draws the scaled popup inset over a dimmed copy of the main page. These
sample the composite's centre (popup) and border (dimmed backdrop) independently to
prove the layering, the dim ramp, the CFR backdrop fill on a sparse VFR main, and
the pre/mid/tail seams; the exact graphs are pinned in ``test_mux_filtergraph.py``.

Input material is generated with ffmpeg's lavfi sources, so the tests need no
fixtures on disk; they are skipped when ffmpeg/ffprobe are not installed. No
shared conftest by design — the shared builders, samplers and the marker block
come from the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import compose_popup_video
from guidebot_recorder.video.mux.probe import probe_duration
from tests.unit.video._mux_helpers import (
    _BORDER,
    _CENTER,
    FFMPEG,
    _assert_dimmed_green,
    _assert_rgb,
    _assert_yellow,
    _make_color_video,
    _make_main_color_timeline,
    _sample_region_rgb,
    _video_codec,
)

pytestmark = FFMPEG


def _make_sparse_vfr_main(path: Path) -> None:
    """Write a 3s green VFR webm whose [1s, 2s) interval has *no* frames.

    Mimics a backgrounded main page: Playwright's VFR screencast can emit zero
    frames while the popup is on top, so a raw ``trim`` of the interval yields an
    empty backdrop. Only CFR normalisation (``fps``) fills it by cloning.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x00ff00:duration=3:size=320x240:rate=25",
            "-vf",
            "select='lt(t,1)+gte(t,2)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "1M",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_compose_popup_video_floating_composites_popup_over_dimmed_main(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    # Full-length film, one H.264 encode.
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    # Pre/tail are the verbatim main page.
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    # The interval is a composite: scaled popup inset, dimmed main at the border.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_floating_zero_transition_ms_renders(tmp_path: Path) -> None:
    # open_ms=0 (a valid "no open animation" config) must not make the dim ramp
    # divide by zero (t/0 -> inf/NaN brightness).
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, open_ms=0, close_ms=0
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))


def test_compose_popup_video_floating_cfr_fills_empty_backdrop(tmp_path: Path) -> None:
    main = tmp_path / "main.webm"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_sparse_vfr_main(main)  # no frames in [1s, 2s)
    _make_color_video(popup, "yellow", 1.0)

    # A raw trim of the interval would be empty; CFR normalisation must fill it,
    # so this renders without the empty-backdrop guard firing.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # The backdrop is present for the full interval (cloned last real frame).
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))


def test_compose_popup_video_floating_no_pre(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Popup opens at t~0: no pre segment, mid + tail only.
    compose_popup_video(main, popup, out, opened_at=0.0, closed_at=1.0, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 0.5, _CENTER))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_floating_no_tail_holds_open(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    # Popup open to end-of-main: no tail, hold the framed popup (no phantom close).
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=2.0,
        closed_at=3.0,
        floating=True,
        hold_open_at_end=True,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    # Held open at the last frame: still the framed popup, still dimmed backdrop.
    # The mid interval [2s, 3s) is main's blue segment, dimmed by the ramp.
    _assert_yellow(_sample_region_rgb(out, 2.9, _CENTER))
    red, green, blue = _sample_region_rgb(out, 2.9, _BORDER)
    assert blue > 50 and blue < 245, f"backdrop blue should be dimmed: {(red, green, blue)}"
    assert red < 70 and green < 70, f"backdrop should stay blue-dominant: {(red, green, blue)}"


def test_compose_popup_video_floating_clamps_short_transition(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.5)

    # span (0.3s) < open_ms + close_ms (0.56s): the fades must clamp, not overrun.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=1.3, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
