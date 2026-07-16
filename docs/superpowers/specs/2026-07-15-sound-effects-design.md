# Sound effects — subtle key + click SFX — design

Date: 2026-07-15
Status: REVIEWED — GO. Two genuine Fable-model review rounds ran on 2026-07-15 (each
round: fresh read-only agents, code-grounded, alimiter/asplit behavior checked on
real ffmpeg); every finding was judged and applied. (The pre-handoff "review #1/#2"
provenance had been confabulated by fork agents and was reset before these real
reviews ran; see docs/superpowers/2026-07-15-video-polish-HANDOFF.md.) Code anchors
re-verified against the current tree on 2026-07-15.
Adds built-in, subtle sound effects (a key tick per typed character and a soft click
on click), mixed under the TTS narration. Opt-in and render-only; no recompile.

## Design decisions (self-review during drafting — NOT a Fable review)

- **assets loading** — assets are loaded via `importlib.resources.as_file`, not `read_text`
  (ffmpeg needs a real filesystem path); the SFX bed uses **two ffmpeg inputs total**
  (`click.wav`, `key.wav`) with `asplit`/`adelay`, not one `-i` per event; and the
  final `-t total` re-trim is **mandatory** (it is what keeps the mixed bed inside
  `mux_audio_tracks`' `abs(audio − video) <= 0.05` tolerance).
- **negative-offset handling** — a negative offset is impossible by construction, so it is
  **fail-loud via an explicit `raise RenderError` (NOT a bare `assert`, which `python -O`
  strips)**, not silently dropped; offsets slightly past `total` are clamped by
  the mandatory `-t total` trim.

## Goal and user-visible acceptance

- When enabled, each typed character plays a short, quiet key tick, and each click
  plays a soft click, mixed beneath the narration on every audio track.
- The sounds are **built in** — no author-supplied files required.
- Off by default: existing renders are byte-for-byte unchanged.

## Config

New render-only block in `models/config.py` (NOT part of `config_hash()`):

```python
class SoundConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False                      # opt-in
    click: bool = True
    keys: bool = True
    volume: float = Field(default=-12.0, le=0)  # dB attenuation on the SFX bed; <= 0
    # `le=0`: only attenuation is allowed. A positive gain would boost the SFX bed and
    # erode the −20 dBFS source headroom the Clipping section relies on.

class Config(BaseModel):
    ...
    sound: SoundConfig = Field(default_factory=SoundConfig)
```

## Assets

Two tiny bundled WAVs shipped as package resources under `guidebot_recorder/sfx/`:

- `click.wav` — soft mouse click, ~60 ms.
- `key.wav` — short soft key tick, ~25 ms.

Both mono, 48 kHz (`SAMPLE_RATE`), 16-bit; the mixer up-mixes to stereo.

**Loading:** ffmpeg needs a real path, so resolve with
`importlib.resources.as_file(files("guidebot_recorder.sfx").joinpath("click.wav"))`
inside a context manager (zip-safe — materializes a temp file if the package is
zipped). The `read_text()` pattern used for `cursor.js` does **not** apply.

A committed generator `scripts/gen_sfx.py` (numpy → WAV) synthesizes them once and
is re-runnable/deterministic — **seed the RNG with a fixed seed** (e.g.
`np.random.default_rng(0)`) so the noise/impulse bursts are byte-identical on every
re-run (the "same bytes on re-run" test depends on it):

- `key`: ~25 ms; a short noise/impulse burst through a gentle low-pass, fast
  exponential decay (attack ~1 ms, decay ~20 ms), low amplitude.
- `click`: ~60 ms; a fuller two-part click (a soft down-transient plus a quieter
  up-transient), band-limited, fast decay.

Document the exact envelope parameters in `gen_sfx.py` so the assets are
reproducible. **Peak-limit both WAVs to ≤ −20 dBFS at the source** (headroom for
`amix` with near-full-scale narration — see Clipping); the `volume` gain attenuates
further. `numpy` is a **script-only** dependency used to regenerate the committed
WAVs — it is *not* a runtime dependency (`pyproject.toml` runtime deps are unchanged);
render consumes the committed `sfx/*.wav`.

**numpy provisioning (review #1):** numpy is absent from both runtime deps and
`[dependency-groups] dev` today, so specify *how* `gen_sfx.py` gets it — declare it
via PEP 723 inline script metadata at the top of `scripts/gen_sfx.py`:

```python
# /// script
# dependencies = ["numpy"]
# ///
```

run with `uv run scripts/gen_sfx.py` (uv provisions numpy in an ephemeral env; no
project dependency is added). The "reproducibly writes the two WAVs" test must be
**skipped when numpy is unavailable** (`pytest.importorskip("numpy")`) so CI on a
clean checkout stays green without a numpy install.

## Event collection (`recorder/render.py`)

`run_render` owns `sfx_events: list[tuple[str, float]]` and a sink (the `on_sfx`
contract — kinds `"click"`/`"key"` — is defined in
`2026-07-15-typing-animation-design.md`):

```python
def sfx_sink(kind: str) -> None:
    sfx_events.append((kind, time.monotonic()))
```

The sink is passed to the render `Recorder` **only when** `cfg.sound.enabled`;
otherwise `on_sfx=None` and assembly is identical to today.

After the step loop, convert to timeline offsets against the existing `anchor`
(render.py ~559): `offset = t - anchor`. The sink is attached only to Recorders
built **inside** the step loop (render.py:606), strictly after `anchor`
(render.py:559), so `offset < 0` cannot happen — **verify it with an explicit
`raise RenderError(...)` on violation** (mirroring `build_audio_bed`'s `ValueError`
guard at audiobed.py:48-50 and `_mux_tracks_for_timeline`'s overrun raise at
render.py:307-310; NOT a bare `assert`, which `python -O` strips), never silently
drop. Offsets can land marginally past
`total` after popup-composite trimming; the mandatory `-t total` trim on the SFX bed
(below) clamps them, so no event lengthens the bed.

These offsets live on the **same main-clock timeline** as narration
(`narration_offset = time.monotonic() - anchor`, render.py ~582), so the popup
compositing path treats SFX exactly like narration (see below).

Gating:
- collect/emit SFX only when `cfg.sound.enabled`;
- keep `"click"` events only if `cfg.sound.click`;
- keep `"key"` events only if `cfg.sound.keys`. (The typing spec already emits `"key"`
  solely on the animated path, so an extra "and typing animated" guard is
  belt-and-suspenders, not required.)

## Mixing (`guidebot_recorder/video/sfx.py`, new)

```python
def build_sfx_bed(
    events: list[tuple[str, float]],   # (kind, offset_seconds)
    total: float,
    out: Path,
    *,
    click_path: Path,
    key_path: Path,
    gain_db: float,
) -> None:
```

Build **one** language-independent SFX bed with a **bounded** filter graph: the
ffmpeg command has **exactly** the silent base plus the two SFX inputs,
regardless of how many events there are —

- input 0: `anullsrc` silence of `total` seconds at `SAMPLE_RATE`;
- input 1: `click.wav`; input 2: `key.wav` (each resolved via `as_file`);
- for each source, count its events, `aresample`/`aformat` to 48000/stereo, `asplit`
  into that many branches, `adelay` each branch to its event offset;
- `amix` the silent base + every delayed branch with `normalize=0`;
- attenuate the mix once with `volume=<gain_db>dB`;
- **`-t total` (mandatory)** to trim/pad to exactly `total` seconds.

The **inputs** stay bounded at 3, but the `filter_complex` *string* grows one
`adelay` branch per event (a long typed sentence → hundreds of branches). Fine for
`ARG_MAX` in practice; if a very long scenario ever risks the command-line limit,
fall back to ffmpeg's `-filter_complex_script <file>` (same graph, read from a file).
Noted as future-proofing, not required for v1.

Per-source event count is normative (review #1): **zero events for a source ⇒ omit
that `-i` entirely** — a labelled `asplit=1` pad that no `amix` consumes fails
ffmpeg's filter-graph validation (unconnected output pad). **Exactly one event ⇒
`adelay` that input directly, no `asplit`.** Two or more ⇒ `asplit=N` then `adelay`
per branch. If there are no events at all across both sources, do not build a bed.
The re-trim is what guarantees the bed length equals `total` so `mux_audio_tracks`'
tolerance holds.

Assembly (`_mux_tracks_for_timeline` / `_assemble_audio_tracks`, render.py ~296–375):

1. If `cfg.sound.enabled` and there is ≥1 event, build `sfx-bed.wav` once in staging.
2. For each language, build the narration bed as today, then mix the shared SFX bed
   into `bed-<lang>.wav` (a two-input `amix normalize=0`, re-trimmed to `total`). The
   SFX bed's `gain_db` is already applied; narration is never attenuated.
3. When `cfg.sound.enabled` is false or there are no events, behaviour is exactly
   today's (no extra input) — byte-identical output for existing scenarios.

### Clipping

TTS narration sits near full scale; `amix` with `normalize=0` can exceed 1.0 on
transients. Defences: source WAVs ≤ −20 dBFS, `volume=<gain_db>dB` on the SFX bed,
and a final `alimiter` on the combined per-language bed in the mix-in step.

**Pin the alimiter flags (review #1 — verified against ffmpeg):** the limiter must be
`alimiter=limit=0.95:level=disabled`. `alimiter`'s `level` option **defaults to
`true`**, which auto-normalizes the output back toward full scale — so a bare
`alimiter=limit=0.95` would re-boost the bed to ~0 dBFS and the `astats` test below
(peak < 0 dBFS) would *fail*. `level=disabled` keeps the 0.95 ceiling as a true
ceiling. (Optionally pin `latency=1`: lookahead-latency compensation also defaults
off; harmless after the mandatory `-t total` re-trim, but explicit is better.) Note
the limiter can touch loud narration transients even away from SFX events; acceptable
because the whole feature is opt-in, but documented so it is not mis-filed later. A
mix test asserts the combined peak stays below 0 dBFS via ffmpeg `astats`.

### Popup composite interaction

The popup path (`compose_popup_video`, then
`_assemble_audio_tracks(..., preencoded=True)`) uses `total = probe_duration(composite)`
and beds spanning that composite timeline. SFX offsets are on the main clock just
like narration offsets, so they are placed by the same rule; the `-t total` trim
clamps any event past the composite end. No popup-specific remapping is introduced.
**Caveat:** clicks *inside* the popup inherit the same bounded encoder-startup/`tpad`
skew as narration (up to ~2 s, mux.py:173-181); a ~60 ms click is more skew-sensitive
than speech, but this stays within the design's approximate-sync (K2) rule — noted so
it is not later mis-filed as a mixing bug.

## Files touched

- `guidebot_recorder/models/config.py` — `SoundConfig`, `Config.sound`.
- `guidebot_recorder/sfx/` — `click.wav`, `key.wav` (bundled), package `__init__.py`.
- `scripts/gen_sfx.py` — deterministic generator.
- `guidebot_recorder/video/sfx.py` (new) — `build_sfx_bed`.
- `guidebot_recorder/recorder/render.py` — event collection, offset conversion (with
  the non-negative assert), gating, and the per-language mix-in.
- `pyproject.toml` — **only if needed (verify first; likely a no-op).** hatchling with
  `packages = ["guidebot_recorder"]` (pyproject.toml:56-57) already ships every
  non-excluded file under the package — that is why `overlay/cursor.js` and
  `chrome/chrome.js` ship today with no explicit include, and `.gitignore` has no
  `*.wav` rule. Confirm the committed `sfx/*.wav` land in `python -m build`'s wheel;
  add an include **only** if they do not. (Consistent with the slides spec, which
  calls the analogous `slide/*.js` include "likely a no-op".)

## Testing

- `scripts/gen_sfx.py` reproducibly writes the two WAVs (same bytes on re-run;
  correct sample rate/length).
- `build_sfx_bed`: output is exactly `total` seconds; energy near each event offset,
  near-silence elsewhere; `gain_db` applied; the ffmpeg command uses **at most three**
  inputs (base + up to 2 assets) regardless of event count — a click-only / key-only
  bed uses **two** inputs (the absent source's `-i` is omitted, per the zero-event
  rule), and a no-event scenario builds no bed at all.
- Event→offset conversion is **fail-loud via explicit `raise`** (a negative offset
  raises `RenderError`; the test must pass under `python -O`, so no bare `assert`).
- A narration + SFX bed still matches the video duration within tolerance; the
  combined bed's peak stays below 0 dBFS (`astats`) thanks to the `alimiter`.
- Gating: `click=False`/`keys=False`/`enabled=False` suppress the expected events.
- Multilingual: the SFX are identical across every language bed.

## Recompile impact

None (render-only; outside `config_hash()`).

## Cross-references

- `2026-07-15-typing-animation-design.md` — defines `on_sfx` and produces the
  `"key"`/`"click"` events this spec consumes.
