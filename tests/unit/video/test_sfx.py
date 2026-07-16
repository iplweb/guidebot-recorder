from importlib.resources import as_file, files
from pathlib import Path

import pytest

pytestmark = pytest.mark.ffmpeg


def _assets():
    return (
        files("guidebot_recorder.sfx").joinpath("click.wav"),
        files("guidebot_recorder.sfx").joinpath("key.wav"),
    )


def test_build_sfx_bed_length_and_bounded_inputs(tmp_path):
    from guidebot_recorder.video.mux import probe_duration
    from guidebot_recorder.video.sfx import build_sfx_bed

    out = tmp_path / "sfx.wav"
    click, key = _assets()
    with as_file(click) as cp, as_file(key) as kp:
        build_sfx_bed(
            [("click", 0.5), ("key", 1.0), ("key", 1.2)], total=3.0, out=out,
            click_path=Path(cp), key_path=Path(kp), gain_db=-12.0)
    assert abs(probe_duration(out) - 3.0) < 0.05


def test_build_sfx_bed_click_only_uses_two_inputs(tmp_path):
    # key source omitted entirely when it has zero events (no unconnected pads)
    from guidebot_recorder.video.sfx import build_sfx_bed

    out = tmp_path / "sfx.wav"
    click, key = _assets()
    with as_file(click) as cp, as_file(key) as kp:
        build_sfx_bed([("click", 0.5)], total=2.0, out=out,
                      click_path=Path(cp), key_path=Path(kp), gain_db=-12.0)
    assert out.exists()


def test_build_sfx_bed_rejects_negative_offset(tmp_path):
    from guidebot_recorder.recorder.render import RenderError
    from guidebot_recorder.video.sfx import build_sfx_bed

    click, key = _assets()
    with as_file(click) as cp, as_file(key) as kp, pytest.raises((ValueError, RenderError)):
        build_sfx_bed([("click", -0.1)], total=2.0, out=tmp_path / "x.wav",
                      click_path=Path(cp), key_path=Path(kp), gain_db=-12.0)
