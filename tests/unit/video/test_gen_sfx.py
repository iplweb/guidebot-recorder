import wave

import pytest


def test_committed_sfx_assets_exist_and_are_48k_mono_16bit():
    from importlib.resources import files

    for name in ("click.wav", "key.wav"):
        p = files("guidebot_recorder.sfx").joinpath(name)
        with wave.open(str(p), "rb") as w:
            assert w.getframerate() == 48000
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2


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
