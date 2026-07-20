# Bundled sound-effect provenance

The two runtime WAV files are derived from studio recordings by Joseph SARDIN,
published by BigSoundBank under CC0 (public domain):

| Runtime file | Selected sound | Source recording |
| --- | --- | --- |
| `key.wav` | K3 — low-profile iMac keyboard | [Quick iMac Keyboard, sound 1731](https://bigsoundbank.com/clavier-imac-rapide-s1731.html) |
| `click.wav` | M1 — Raspberry mouse press and release | [Raspberry Mouse, Single Click, sound 1735](https://bigsoundbank.com/souris-raspberry-simple-clic-s1735.html) |

BigSoundBank marks both source pages **CC0 (public domain)** and permits copying,
modification, redistribution, and commercial use without attribution. Attribution
is retained here as good practice. Sources were retrieved on 2026-07-20.

The repository keeps only short mono 48 kHz excerpts in `scripts/sfx_sources/`:

- keyboard: samples 7,680–11,520 (0.160–0.240 s) from sound 1731;
- mouse: samples 5,712–14,112 (0.119–0.294 s) from sound 1735.

`uv run scripts/gen_sfx.py` applies a gentle high-pass filter, boundary fades, DC
removal, and peak-normalization to −20 dBFS, then writes deterministic mono 48 kHz
16-bit PCM runtime assets. The render mixer provides the final mouse/key balance
and applies the scenario's `sound.volume` attenuation.
