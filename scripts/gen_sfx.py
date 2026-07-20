# /// script
# dependencies = ["numpy"]
# ///
"""Build the bundled UI sounds from the selected CC0 recordings.

Run with ``uv run scripts/gen_sfx.py``.  Fixed source excerpts and deterministic
processing make ``guidebot_recorder/sfx/{click,key}.wav`` byte-identical between
runs.

The short source excerpts in ``scripts/sfx_sources`` come from Joseph SARDIN's
CC0 recordings on BigSoundBank: an iMac keyboard (sound 1731) and Raspberry mouse
(sound 1735).  This script removes sub-bass/rumble, fades the excerpt boundaries,
and restores the project's 20 dB source headroom.  See ``guidebot_recorder/sfx``
for complete provenance.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
PEAK_DBFS = -20.0
SOURCE_DIR = Path(__file__).with_name("sfx_sources")
SOURCE_FILES = {
    "click": SOURCE_DIR / "click-raspberry-m1.wav",
    "key": SOURCE_DIR / "key-imac-k3.wav",
}


def _normalise(sig: np.ndarray) -> np.ndarray:
    """Remove DC and peak-normalise while retaining 20 dB of source headroom."""

    sig = sig - np.mean(sig)
    peak = np.max(np.abs(sig)) or 1.0
    target = 10 ** (PEAK_DBFS / 20)
    return sig * (target / peak)


def _write(path: Path, sig: np.ndarray) -> None:
    data = (np.clip(_normalise(sig), -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def _read_source(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as source:
        actual = (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
        )
        expected = (SR, 1, 2)
        if actual != expected:
            raise ValueError(f"{path} must be 48 kHz mono 16-bit PCM, got {actual}")
        frames = source.readframes(source.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0


def _high_pass(sig: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """Apply a deterministic one-pole high-pass to remove recording rumble."""

    result = np.zeros_like(sig)
    dt = 1.0 / SR
    rc = 1.0 / (2 * np.pi * cutoff_hz)
    alpha = rc / (rc + dt)
    for index in range(1, len(sig)):
        result[index] = alpha * (result[index - 1] + sig[index] - sig[index - 1])
    return result


def _prepare(path: Path, *, cutoff_hz: float) -> np.ndarray:
    sig = _high_pass(_read_source(path), cutoff_hz)
    fade_in = min(len(sig), int(SR * 0.003))
    fade_out = min(len(sig), int(SR * 0.008))
    sig[:fade_in] *= np.linspace(0.0, 1.0, fade_in, endpoint=False)
    sig[-fade_out:] *= np.linspace(1.0, 0.0, fade_out)
    return sig


def generate(out_dir: Path) -> None:
    _write(out_dir / "click.wav", _prepare(SOURCE_FILES["click"], cutoff_hz=100))
    _write(out_dir / "key.wav", _prepare(SOURCE_FILES["key"], cutoff_hz=160))


if __name__ == "__main__":
    generate(Path("guidebot_recorder/sfx"))
    print("wrote guidebot_recorder/sfx/{click,key}.wav from CC0 recordings")
