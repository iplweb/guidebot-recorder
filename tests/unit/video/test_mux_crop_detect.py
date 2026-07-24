"""ffmpeg-backed tests for ``video.mux.crop`` — the ``detect_*`` pixel heuristics.

When neither the ``window.open`` features nor the popup's own content bounding box
state a geometry, the recording is the last witness: the real window is the region
that is *not* the flat filler Playwright pads the canvas with. These cover
``detect_content_crop`` (finding the window, and declining on a full frame, an
unstable rect, ink on a full-bleed page, a missing file, and a wedged ffmpeg — with
the pass budget) and ``detect_teardown_tail`` (measuring a shrinking window, and
declining on a stable recording, a canvas-wide crop, an implausible run, a bad file).

Unlike the fail-loud rest of the package, these degrade to "no crop" on *any*
failure — so two tests carry a sentinel proving the ffmpeg pass actually ran, and
the seam is patched on the *defining* module (see ``test_mux_seams.py``).

No shared conftest by design — the shared builders and the marker block come from
the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import detect_content_crop, detect_teardown_tail
from tests.unit.video._mux_helpers import (
    FFMPEG,
    _make_color_video,
    _make_popup_with_filler,
    _make_popup_with_teardown_tail,
)

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


def _make_popup_with_shifting_filler(path: Path, seconds: float) -> None:
    """Write a 320x240 popup whose content region changes size over time.

    No rect is stable across frames, so the consensus must be refused rather than
    letting one frame's answer decide the whole composite.
    """
    third = seconds / 3
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
            f"color=c=yellow:duration={seconds}:size=300x220:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=200x150:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=100x70:rate=25",
            "-filter_complex",
            (
                f"[0:v][1:v]overlay=x=0:y=0:enable='lt(t,{third:.3f})'[a];"
                f"[a][2:v]overlay=x=0:y=0:"
                f"enable='between(t,{third:.3f},{2 * third:.3f})'[b];"
                f"[b][3:v]overlay=x=0:y=0:enable='gt(t,{2 * third:.3f})',"
                "format=yuv420p[outv]"
            ),
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


def _make_full_bleed_popup_with_ink(path: Path, seconds: float) -> None:
    """Write a 320x240 popup that fills its canvas, with darker ink inside it.

    The real shape of a featureless ``window.open``: no padding anywhere, the
    page's own background in every corner, and content painted on top.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:duration={seconds}:size=120x60:rate=25",
            "-filter_complex",
            "[0:v][1:v]overlay=x=40:y=50,format=yuv420p[outv]",
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


def test_detect_content_crop_finds_the_window_inside_flat_filler(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.2)

    # The filler is mid-grey, not black, so a plain cropdetect would see nothing:
    # the detection keys on "differs from the padding colour", not on darkness.
    assert detect_content_crop(popup) == (160, 120, 0, 0)


def test_detect_content_crop_declines_on_a_full_frame_recording(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_color_video(popup, "yellow", 1.2)

    # Nothing to trim: no crop at all rather than a bogus one.
    assert detect_content_crop(popup) is None


def test_detect_content_crop_declines_without_a_stable_rect(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_shifting_filler(popup, 1.5)

    # Taking the first frame's answer here would make the framed window jump.
    assert detect_content_crop(popup) is None


def test_detect_content_crop_declines_on_ink_inside_a_full_bleed_page(tmp_path: Path) -> None:
    """Ink floating inside a full-bleed page is not a window inside padding.

    Regression from a real recording: a featureless ``window.open`` filled the
    whole canvas, so the corner pixel sampled the *page's* background and the
    detection happily "trimmed" everything except the text. Playwright always
    anchors the popup at the top-left, so a rect that does not start at the
    origin is proof the reading is bogus.
    """
    popup = tmp_path / "popup.mp4"
    _make_full_bleed_popup_with_ink(popup, 1.2)

    assert detect_content_crop(popup) is None


def test_detect_content_crop_declines_on_a_missing_file(tmp_path: Path) -> None:
    # A last-resort heuristic must never abort a render that would otherwise
    # simply not crop.
    assert detect_content_crop(tmp_path / "absent.mp4") is None


def test_detect_content_crop_declines_when_ffmpeg_overruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.0)
    # `detect_content_crop` degrades to None on ANY failure, so the assertion
    # below also holds when the patch reaches nobody — the test would pass while
    # proving nothing and quietly shelling out to real ffmpeg. The sentinel is
    # what makes the timeout the actual subject: no call, no test.
    #
    # It has to be a sentinel on the *ffmpeg* passes specifically. Timing out on
    # the first `_run` of any kind would fire inside `probe._probe_all`'s ffprobe
    # — a different module, with a seam of its own — and short-circuit before
    # crop.py ever runs ffmpeg, so `called` would attest to probe.py's seam and
    # say nothing about crop.py's. Let ffprobe through to the real runner and
    # wedge only ffmpeg.
    called: list[list[str]] = []
    real_run = mux_module.ffmpeg._run

    def timing_out(cmd, **kwargs):
        called.append(cmd)
        if Path(cmd[0]).name != "ffmpeg":
            return real_run(cmd, **kwargs)
        assert kwargs.get("timeout") == mux_module.CROPDETECT_TIMEOUT, (
            "every detection pass must carry the timeout"
        )
        raise subprocess.TimeoutExpired(cmd, mux_module.CROPDETECT_TIMEOUT)

    monkeypatch.setattr(mux_module.ffmpeg, "_run", timing_out)

    # A wedged ffmpeg costs the crop, never the render.
    crop = detect_content_crop(popup)
    # Asserted before the result, so a broken seam reports itself instead of
    # surfacing as a puzzling "expected None, got a perfectly good rect".
    assert any(Path(command[0]).name == "ffmpeg" for command in called), (
        f"crop.py's own ffmpeg pass never reached the patched _run: the seam in "
        f"guidebot_recorder.video.mux.crop is broken (saw {called})"
    )
    assert crop is None


def test_detect_content_crop_passes_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.0)
    timeouts: list[float | None] = []
    real_run = mux_module.ffmpeg._run

    def spy_run(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mux_module.ffmpeg, "_run", spy_run)

    assert detect_content_crop(popup) == (160, 120, 0, 0)
    # Three passes carry the budget, not just one of them: the ffprobe, the
    # padding sample and the cropdetect run. Counting them is what makes this
    # test notice a broken seam — the crop degrades to a plain result whether the
    # spy was reached or not, so only the tally distinguishes the two.
    assert len(timeouts) >= 3, timeouts
    assert all(value == mux_module.CROPDETECT_TIMEOUT for value in timeouts), timeouts


def test_detect_teardown_tail_measures_the_trailing_shrunken_frames(tmp_path):
    popup = tmp_path / "popup.mp4"
    _make_popup_with_teardown_tail(popup, good_seconds=0.8, tail_seconds=0.2)

    assert detect_teardown_tail(popup, (200, 150, 0, 0)) == pytest.approx(0.2, abs=0.05)


def test_detect_teardown_tail_reports_nothing_for_a_stable_recording(tmp_path):
    popup = tmp_path / "popup.mp4"
    _make_popup_with_teardown_tail(popup, good_seconds=1.0, shrunk=None)

    assert detect_teardown_tail(popup, (200, 150, 0, 0)) == 0.0


def test_detect_teardown_tail_declines_when_the_crop_covers_the_canvas(tmp_path):
    # No padding anywhere means no filler to recognise a shrunken window against.
    popup = tmp_path / "popup.mp4"
    _make_popup_with_teardown_tail(popup, good_seconds=0.8, tail_seconds=0.2)

    assert detect_teardown_tail(popup, (320, 240, 0, 0)) == 0.0


def test_detect_teardown_tail_refuses_an_implausibly_long_run(tmp_path):
    # Almost the whole recording reads as filler: the sampled corner is not
    # reporting a window at all, so the measurement is discarded rather than
    # trimming the popup away.
    popup = tmp_path / "popup.mp4"
    _make_popup_with_teardown_tail(popup, good_seconds=0.1, tail_seconds=0.9)

    assert detect_teardown_tail(popup, (200, 150, 0, 0)) == 0.0


def test_detect_teardown_tail_degrades_on_an_unreadable_file(tmp_path):
    missing = tmp_path / "nope.webm"

    assert detect_teardown_tail(missing, (200, 150, 0, 0)) == 0.0
