"""ffmpeg-backed tests for ``video.mux.crop`` wired through ``compose_popup_video``.

Playwright's ``record_video_size`` is context-level, so a popup records onto a
full-viewport canvas: its real window sits top-left, the rest is filler. ``popup_crop``
trims that filler *before* the scale (float only), so the rounded corners, fade and
shadow are computed on the real window. These cover the crop's effect on the picture,
its placement/parity in the graph, the no-op cases, cut ignoring it, out-of-frame
rejection, the level-3 heuristic feeding compose, and teardown-tail replacement.

Input material is generated with ffmpeg's lavfi sources, so the tests need no
fixtures on disk; they are skipped when ffmpeg/ffprobe are not installed. No
shared conftest by design — the shared builders, samplers and the marker block
come from the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guidebot_recorder.video.mux import compose_popup_video, detect_content_crop
from guidebot_recorder.video.mux.probe import probe_duration
from tests.unit.video._mux_helpers import (
    _BORDER,
    FFMPEG,
    _assert_dimmed_green,
    _assert_yellow,
    _make_color_video,
    _make_main_color_timeline,
    _make_popup_with_filler,
    _make_popup_with_teardown_tail,
    _sample_region_rgb,
    capture_ffmpeg_args,
    filtergraph_of,
)

pytestmark = FFMPEG

# A region that lands inside the framed popup only once the filler is cropped
# away. Uncropped, the 320x240 popup scales to 230x172 centred at x=45..275 and
# the filler (source x>160 -> screen x>=160) covers this strip.
_CROPPED_RIGHT = "16:16:186:112"


def _popup_chain(filters: str) -> str:
    """Return the ``[popup_cut]`` consumer link of a filtergraph."""
    (chain,) = [part for part in filters.split(";") if part.startswith("[popup_cut]")]
    return chain


def test_compose_popup_video_float_crops_popup_to_its_content(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(160, 120, 0, 0)
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # The framed window is now wall-to-wall content: no grey filler inside it.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CROPPED_RIGHT))
    # Outside the (now smaller) window the dimmed main page still shows.
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_float_without_crop_keeps_the_filler(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    # Back-compat: no geometry supplied -> today's full-canvas scaling, filler and all.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    rgb = _sample_region_rgb(out, 1.5, _CROPPED_RIGHT)
    red, green, blue = rgb
    assert not (red > 170 and green > 170 and blue < 90), f"expected grey filler: {rgb}"


def test_compose_popup_video_float_crop_precedes_scale_and_is_even(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = capture_ffmpeg_args(monkeypatch)

    # Odd numbers everywhere: yuv420p needs even dimensions, so they must snap down.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(161, 121, 3, 5)
    )

    chain = _popup_chain(filtergraph_of(seen[0]))
    assert "crop=160:120:2:4," in chain
    assert chain.index("crop=") < chain.index("scale=")


def test_compose_popup_video_float_without_crop_emits_no_crop_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = capture_ffmpeg_args(monkeypatch)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    assert "crop=" not in _popup_chain(filtergraph_of(seen[0]))


def test_compose_popup_video_float_full_frame_crop_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = capture_ffmpeg_args(monkeypatch)

    # A popup whose requested window is at least the whole canvas must not gain a
    # redundant crop filter.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(400, 300, 0, 0)
    )

    assert "crop=" not in _popup_chain(filtergraph_of(seen[0]))


def test_compose_popup_video_cut_ignores_popup_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = capture_ffmpeg_args(monkeypatch)

    # cut/slide show the popup full-frame; cropping is a float-only cosmetic.
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.0,
        transition="cut",
        popup_crop=(160, 120, 0, 0),
    )

    assert "crop=" not in filtergraph_of(seen[0])


def test_compose_popup_video_rejects_out_of_frame_crop(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    with pytest.raises(ValueError, match="popup_crop"):
        compose_popup_video(
            main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(0, 120, 0, 0)
        )


def test_detect_content_crop_result_feeds_compose_popup_video(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    crop = detect_content_crop(popup)
    assert crop is not None
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=crop
    )

    # Same framing as the deterministic level-1 crop: filler gone, backdrop kept.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CROPPED_RIGHT))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_holds_the_last_good_frame_instead_of_the_teardown_tail(tmp_path):
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(main, "blue", 3.0)
    _make_popup_with_teardown_tail(popup, good_seconds=0.8, tail_seconds=0.2)

    compose_popup_video(
        main,
        popup,
        out,
        1.0,
        2.0,
        transition="float",
        popup_crop=(200, 150, 0, 0),
    )

    # The composite keeps its full length — the tail is replaced, not dropped.
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
