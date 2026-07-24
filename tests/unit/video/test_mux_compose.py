"""ffmpeg-backed tests for ``video.mux.compose`` — the ``compose_popup_video`` entry.

Covers the baseline hard-cut path (main → full-frame popup → main), the encoder
startup-gap padding and its rejection, the visual-ready discard, and the
``transition`` / ``floating`` dispatch onto the cut / float / slide builders. The
per-mode picture is asserted in ``test_mux_floating.py`` / ``test_mux_slide.py``;
the exact graphs are pinned in ``test_mux_filtergraph.py``.

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
    _make_video,
    _sample_region_rgb,
    _sample_rgb,
    _video_codec,
)

pytestmark = FFMPEG


def _make_popup_with_bad_leading_frames(path: Path) -> None:
    """Write magenta pre-prime frames followed by a verified yellow interval."""

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=magenta:duration=0.2:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=yellow:duration=0.8:size=320x240:rate=25",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[outv]",
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


def test_compose_popup_video_switches_main_popup_main(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_omits_tail_when_popup_stays_open(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=3.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    # The last second must still be the popup, not main's blue tail.
    _assert_rgb(_sample_rgb(out, 2.5), (255, 255, 0))


def test_compose_popup_video_pads_bounded_encoder_startup_gap(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.92)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 1.1), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 1.8), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_discards_frames_before_visual_prime(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_bad_leading_frames(popup)

    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.2,
        visual_ready_delay=0.4,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 1.3), (0, 255, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_rejects_large_encoder_gap(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_video(main, 4.0)
    _make_color_video(popup, "yellow", 0.5)

    with pytest.raises(ValueError, match="startup gap"):
        compose_popup_video(main, popup, out, opened_at=0.5, closed_at=3.5)


def test_compose_popup_video_floating_false_is_a_hard_cut(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Default floating=False: the interval is a full-frame popup, no backdrop.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=False)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_transition_cut_matches_default(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # transition="cut" reproduces today's hard cut (main -> full-frame popup -> main).
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, transition="cut")

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_transition_float_matches_floating(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # transition="float" reproduces Spec B: scaled popup inset over dimmed main.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, transition="float")

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_explicit_transition_overrides_floating(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # An explicit transition wins over the deprecated floating alias.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, transition="cut"
    )

    # Hard cut: the border is full popup yellow, not the dimmed backdrop float draws.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))
