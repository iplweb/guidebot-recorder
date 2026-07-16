# /// script
# dependencies = ["numpy"]
# ///
"""Deterministic generator for the two bundled SFX WAVs. Run: uv run scripts/gen_sfx.py
Regenerates guidebot_recorder/sfx/{click,key}.wav. Fixed RNG seed → byte-identical."""
from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

SR = 48000
PEAK_DBFS = -20.0


def _limit(sig: np.ndarray) -> np.ndarray:
    peak = np.max(np.abs(sig)) or 1.0
    target = 10 ** (PEAK_DBFS / 20)
    return sig * (target / peak)


def _write(path: Path, sig: np.ndarray) -> None:
    data = (np.clip(_limit(sig), -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack(f"<{len(data)}h", *data.tolist()))


def _key(rng) -> np.ndarray:
    n = int(SR * 0.025)  # ~25 ms
    t = np.arange(n) / SR
    env = np.exp(-t / 0.006)  # fast decay (~1 ms attack ignored, ~20 ms tail)
    noise = rng.standard_normal(n)
    # gentle low-pass: cumulative smoothing
    lp = np.convolve(noise, np.ones(8) / 8, mode="same")
    return _limit(lp * env)


def _click(rng) -> np.ndarray:
    n = int(SR * 0.060)  # ~60 ms
    t = np.arange(n) / SR
    down = np.exp(-t / 0.008) * rng.standard_normal(n)
    up = 0.6 * np.exp(-np.clip(t - 0.012, 0, None) / 0.010) * rng.standard_normal(n)
    band = np.convolve(down + up, np.ones(6) / 6, mode="same")
    return _limit(band)


def generate(out_dir: Path) -> None:
    rng = np.random.default_rng(0)  # fixed seed → byte-identical
    _write(out_dir / "click.wav", _click(rng))
    _write(out_dir / "key.wav", _key(rng))


if __name__ == "__main__":
    generate(Path("guidebot_recorder/sfx"))
    print("wrote guidebot_recorder/sfx/{click,key}.wav")
