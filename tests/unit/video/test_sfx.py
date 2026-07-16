import re
import subprocess
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

import pytest

pytestmark = pytest.mark.ffmpeg


def _assets():
    return (
        files("guidebot_recorder.sfx").joinpath("click.wav"),
        files("guidebot_recorder.sfx").joinpath("key.wav"),
    )


@dataclass
class _FakeSegment:
    """Structural stand-in for tts.base.Segment (path + duration)."""

    path: Path
    duration: float


def _make_full_scale_tone(path: Path, seconds: float, freq: int = 440) -> None:
    """A near-0dBFS stereo sine tone — loud enough to exercise the amix/alimiter ceiling.

    ffmpeg's plain ``sine`` source peaks around -18 dBFS, so it is boosted
    (``volume=8``) to sit right at the ceiling. ``pan=stereo|c0=c0|c1=c0`` duplicates
    the mono signal into both channels without the ~-3 dB power-preserving downmix
    that a plain ``aformat=channel_layouts=stereo``/``-ac 2`` upmix would apply, so
    the bed `build_audio_bed` produces from this source stays right at the ceiling.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}:sample_rate=48000",
            "-af",
            "volume=8,pan=stereo|c0=c0|c1=c0",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _overall_peak_level_db(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(path),
            "-af",
            "astats=metadata=1",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"Overall.*?Peak level dB:\s*(-?[\d.]+)", proc.stderr, re.DOTALL)
    assert match is not None, proc.stderr
    return float(match.group(1))


def test_mix_sfx_into_bed_no_clip_and_duration(tmp_path):
    from guidebot_recorder.video.audiobed import Placed, build_audio_bed
    from guidebot_recorder.video.mux import probe_duration
    from guidebot_recorder.video.sfx import build_sfx_bed, mix_sfx_into_bed

    total = 3.0
    tone = tmp_path / "tone.wav"
    _make_full_scale_tone(tone, total)
    narr = tmp_path / "narr.wav"
    build_audio_bed([Placed(segment=_FakeSegment(path=tone, duration=total), offset=0.0)],
                     total, narr)

    sfx = tmp_path / "sfx.wav"
    click, key = _assets()
    with as_file(click) as cp, as_file(key) as kp:
        build_sfx_bed([("click", 0.5), ("key", 1.0), ("key", 1.2)], total=total, out=sfx,
                      click_path=Path(cp), key_path=Path(kp), gain_db=0.0)

    out = tmp_path / "mixed.wav"
    mix_sfx_into_bed(narr, sfx, out, total)

    assert abs(probe_duration(out) - total) < 0.05
    peak_db = _overall_peak_level_db(out)
    assert peak_db < 0.0, f"expected peak below 0 dBFS (ceiling held), got {peak_db}"


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
