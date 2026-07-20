import math
import wave
from pathlib import Path

import pytest


def test_committed_sfx_assets_exist_and_are_48k_mono_16bit():
    from importlib.resources import files

    for name in ("click.wav", "key.wav"):
        p = files("guidebot_recorder.sfx").joinpath(name)
        with wave.open(str(p), "rb") as w:
            assert w.getframerate() == 48000
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2


@pytest.mark.parametrize(
    ("name", "duration"),
    [("click-raspberry-m1.wav", 0.175), ("key-imac-k3.wav", 0.080)],
)
def test_cc0_source_excerpts_are_present_and_canonical(name, duration):
    path = Path("scripts/sfx_sources") / name
    with wave.open(str(path), "rb") as source:
        assert source.getframerate() == 48000
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getnframes() / source.getframerate() == pytest.approx(
            duration, abs=1 / 48000
        )


def _wav_stats(path):
    with wave.open(str(path), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
        samples = memoryview(frames).cast("h")
        peak = max(abs(sample) for sample in samples) / 32767
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32767
        return wav.getnframes() / wav.getframerate(), 20 * math.log10(peak), 20 * math.log10(rms)


def test_committed_sfx_have_physical_lengths_and_use_source_headroom():
    from importlib.resources import files

    click_duration, click_peak, click_rms = _wav_stats(
        files("guidebot_recorder.sfx").joinpath("click.wav")
    )
    key_duration, key_peak, key_rms = _wav_stats(
        files("guidebot_recorder.sfx").joinpath("key.wav")
    )

    assert click_duration == pytest.approx(0.175, abs=1 / 48000)
    assert key_duration == pytest.approx(0.080, abs=1 / 48000)
    assert click_peak == pytest.approx(-20.0, abs=0.05)
    assert key_peak == pytest.approx(-20.0, abs=0.05)
    # Guard against accidentally returning to near-silent, single-sample impulses.
    # The click contains a real press/release gap, so whole-file RMS includes a
    # deliberate quiet section between the two contacts.
    assert click_rms > -48.0
    assert key_rms > -48.0


def test_gen_sfx_is_byte_deterministic(tmp_path):
    pytest.importorskip("numpy")
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_sfx", "scripts/gen_sfx.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = tmp_path / "a"
    b = tmp_path / "b"
    mod.generate(a)
    mod.generate(b)
    assert (a / "click.wav").read_bytes() == (b / "click.wav").read_bytes()
    assert (a / "key.wav").read_bytes() == (b / "key.wav").read_bytes()

    from importlib.resources import files

    assert (a / "click.wav").read_bytes() == files("guidebot_recorder.sfx").joinpath(
        "click.wav"
    ).read_bytes()
    assert (a / "key.wav").read_bytes() == files("guidebot_recorder.sfx").joinpath(
        "key.wav"
    ).read_bytes()
