# Demo scenario + rollout — integration design

Date: 2026-07-15
Status: REVIEWED — GO (integrated batch). Two genuine Fable-model review rounds ran on
2026-07-15 (each round: fresh read-only agents, code-grounded; round 2 included a
cross-spec composition check); every finding was judged and applied. (The pre-handoff
"review #1/#2" provenance had been confabulated by fork agents and was reset before
these real reviews ran; see docs/superpowers/2026-07-15-video-polish-HANDOFF.md.) Code
anchors re-verified against the current tree on 2026-07-15.
Ties together the four feature specs (cursor-visibility, typing-animation,
sound-effects, slides-intro): how the new `config` blocks compose, what needs
recompilation, the `s.yaml` demo update, back-compat, and docs. Normative only for
how the pieces combine and roll out; each block's own spec is normative for that
block.

## Design decisions (self-review during drafting — NOT a Fable review)

- **teach recompile row** — the recompile matrix's narration row was wrong for `teach`: editing a
  `teach` sentence changes `fingerprint.compiled_from` and **does** require a
  recompile. Split into a render-only `say`/`translations` row and a recompile
  `teach` row.
- **untracked working files** — `s.yaml` / `s.compiled.yaml` are untracked working files; the
  spec now states this batch does not commit them.

## Consolidated config surface

```yaml
config:
  title: "Logowanie do systemu"
  viewport: { width: 1376, height: 800 }
  locale: pl-PL
  tts: { provider: edge, voice: pl-PL-MarekNeural, lang: pl-PL }

  cursor:
    width: 46
    height: 62
    click: { color: "#22d3ee", scale: 4.5, flash: true }
    # cursor now starts centered (see cursor spec)
  typing:
    animate: true
    speed: 60          # ms per character
  sound:
    enabled: true
    click: true
    keys: true
    volume: -12
  intro:
    enabled: true
    subtitle: "Krok po kroku"
    notes: "Materiał szkoleniowy"
  chrome:
    enabled: true
```

Each block is normative in its own spec: `cursor.click` →
`2026-07-15-cursor-visibility-design.md`; `typing` →
`2026-07-15-typing-animation-design.md`; `sound` →
`2026-07-15-sound-effects-design.md`; `intro` (+ the `slide` step) →
`2026-07-15-slides-intro-design.md`; `chrome` is existing.

## Recompile matrix

| Change | `compile` needed? | Why |
|---|---|---|
| `cursor` (size, `click`), centered start | No | Cosmetic, outside `config_hash()`. |
| `typing` (animate/speed) | No | Typed value identical to `fill()`. |
| `sound` (enable/flags/volume) | No | Render-only audio mixing. |
| `intro` (enable/subtitle/notes) | No | Render-only card; no new step. |
| `chrome` (enable/appearance) | No | Existing render-only bar. |
| `say` text / `translations` | No | Render-only narration. |
| Editing a `teach` sentence's text | **Yes** | `teach` text is `fingerprint.compiled_from` (render.py:423); the preflight rejects a mismatch. |
| Adding/removing/reordering a `slide` **step** | **Yes** | Step count / compiled slot alignment change. |
| Editing a target step's **instruction** (`into`/`click`/`hover`/`teach`/`wait.until`) or its `wait.state` | **Yes** | `into`/`click`/`hover`/`teach`/`wait.until` feed `fingerprint.compiled_from` (render.py:385-397); `wait.state` is checked separately via `fingerprint.state == expected_state` (render.py:418/425). Either way the preflight rejects a mismatch (render.py:480-482). |
| Editing **`enterText.text` alone** (value only, same `into`) | No | The typed value is NOT in the fingerprint (`_compiled_from` returns only `enter_text.into`, render.py:393-394); render reads the value live from the step (render.py:845). Picked up **silently**, no recompile, no loud failure. |
| Switching a step's command kind (e.g. `teach` → `enterText`, the demo popup step) | **Yes** | `fingerprint.command_kind` changes → preflight rejects (render.py:422). |

Render preflights the compiled sidecar (source name render.py:468-471, slot count
:472-473, compiler version :474-478, per-step fingerprints :480-482) before
synthesis/recording, so a structural change without recompile fails loudly.

## `s.yaml` demo update (repo root)

1. Enable the render-only toggles above (`cursor`/`typing`/`sound`/`intro`). Note the
   working `s.yaml` already has `chrome.enabled: true` (s.yaml:7-8), so `chrome` is a
   no-op there — listed for completeness only.
2. Simplify the pop-up step. The current
   `teach: "przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl"`
   mixes an unnecessary "switch to popup" instruction (pop-ups are auto-followed —
   see `2026-07-14-popup-multiwindow-design.md`) with literal typing.

   **Recommended** replacement: an explicit, structural field entry

   ```yaml
   - enterText: { into: "pole email", text: "koparka@poczta.wp.pl" }
     say: "Wpisuję adres e-mail w wyskakującym oknie."
   ```

   Rationale: `enterText` is the intended structural path for entering a value; it
   keeps the instruction free of control verbs, avoids relying on the compiler to
   infer a literal `type` from a `teach` sentence, and the value here is non-secret
   so it may appear in the scenario. (A clean literal `teach: "wpisz w pole email
   koparka@poczta.wp.pl"` *can* also work, but it additionally depends on the Reasoner
   returning that exact literal and the field carrying no sensitive metadata, so it is
   not guaranteed-equivalent; `enterText` is preferred for a value.)

3. Optionally prepend a `slide` step and/or rely on `intro.enabled` for the opener.

Because step 2 changes a target step (and any new `slide` step changes the step
count), `s.yaml` must be recompiled with Codex (`guidebot compile s.yaml`) before
`render`; the pure-config toggles alone are render-only.

**Risk note (typing into third-party forms):** the demo enables `typing.animate`, so
the popup email is typed character-by-character with real key events into the onet.pl
login form. Autocomplete/live-validation there can differ from what compile froze (see
the typing spec's render/compile divergence). The trailing value-correction guards the
final value; if the field misbehaves under real keystrokes, set `typing.animate: false`
for the demo.

## Back-compat

Every new block defaults to inert: `sound.enabled=false`, `typing.animate=false`,
`intro.enabled=false`, `chrome.enabled=false`, and both the code cursor size
defaults (34×46) and the `cursor.click` defaults (today's ripple colour/scale, no
flash) are unchanged, so existing scenarios render identically — with **one intended
exception**: the cursor now starts centered instead of top-left for all renders (per
the cursor spec).

## Implementation ordering (shared-file contention)

All four feature specs touch `models/config.py`. Four of five touch
`recorder/render.py`: the typing/sound/slides edits cluster in the same `run_render`
region ~555–606 (intro bootstrap, Recorder construction, SFX sink, slide handling),
while the **cursor** spec's render.py edit is the single line 513
(`Overlay(cfg.cursor)` → `Overlay(cfg.cursor, cfg.viewport)`), *outside* that region.
`overlay/overlay.py` + `overlay/cursor.js` are touched by **both** the cursor spec
(ripple/flash + `CFG.start`) and the slides spec (`hide`/`show` + persistent `hidden`
flag). `recorder/recorder.py`'s `_point_and_prepare` is touched by the typing spec
(`click_sound`) and consumed by the cursor spec (`ripple(flash=)`). To avoid churn
and drift:

1. Land all model/config additions first as one foundation: `CursorClick`,
   `TypingConfig`, `SoundConfig`, `IntroConfig` in `models/config.py`, and the
   `Slide`/`Step` changes in `models/scenario.py`.
2. Then sequence the `recorder/render.py` work: **typing** (Recorder wiring) → **sound**
   (SFX sink + assembly) → **slides/intro** (loop card logic). Each rebases on the
   previous; the specs' `~line` references are accurate today but drift after the first
   merge — re-locate by symbol, not line number.
3. The `overlay/cursor.js` + `overlay/overlay.py` changes from the cursor and slides
   specs must be coordinated by a single owner and land as **one bundle**:
   `ripple(flash)` + `CFG.start` seed (cursor) **together with** `hide`/`show` + the
   persistent `hidden`-flag (slides). This bundle must land **before** the slides
   render.py card logic (the last item in step 2), because that logic calls
   `overlay.hide`/`overlay.show` and `chrome.hide`/`chrome.show` — they must exist
   first. The precondition therefore also includes the **`chrome/chrome.py` +
   `chrome/chrome.js` `hide`/`show` + persistent `hidden`-flag** (slides spec,
   single-owner — no cross-owner coordination needed, but it must precede the slides
   render.py card logic just like the overlay bundle). (It also transitively lands
   before/with typing via step 4's `ripple(flash)`↔typing pairing.)
4. **Cross-file ordering dependency (recorder.py ↔ overlay.py):** the typing spec's
   `_point_and_prepare` edit calls `self.overlay.ripple(self.page, flash=click_sound)`,
   which requires `Overlay.ripple` to already accept the keyword-only `flash` param
   added by the cursor spec. So the cursor spec's `Overlay.ripple(page, *, flash=False)`
   signature change must land **before or together with** the typing spec's
   `_point_and_prepare` rewrite, or render will raise `TypeError: unexpected keyword
   argument 'flash'`. Land the recorder.py `click_sound` threading and the overlay
   `ripple(flash)` param in the same step (both are small).

Parallelism bound: the model/config foundation plus the isolated new/leaf files can
proceed in parallel — `video/sfx.py`, `slide/` (`slide.js`+`slide.py`), `sfx/` assets
+ `scripts/gen_sfx.py`, the `cursor.js` ripple, and the `chrome/chrome.js` hidden-flag;
the shared `render.py`/`config.py` edits serialize per steps 1–2 above.

## Docs

Update bilingually (the repo maintains en + pl):

- `docs/en/scenario-reference.md`, `docs/pl/scenario-reference.md` — new `config`
  blocks (`cursor.click`, `typing`, `sound`, `intro`) and the `slide` step.
- `docs/en/scenario-files.md`, `docs/pl/scenario-files.md` — narrative examples.
- `README.md` — the new blocks, the `slide` step, and the sound/typing/intro
  features in both the English and Polish halves.

## Examples

Correction (review #1): `examples/` currently ships **only** `*.scenario.yaml` (plus
one render-set file) — **no** committed `*.compiled.yaml` sidecars exist in the tree
(the only `*.compiled.yaml` is the untracked repo-root `s.compiled.yaml`). So there is
no "stale sidecar to update"; a slide-bearing example would instead need a
**first-ever committed** sidecar (scenario + freshly recompiled compiled.yaml added
together). Prefer keeping the existing examples untouched and the current
ship-source-only practice (users run `guidebot compile`); demonstrate the new features
in `s.yaml` (and, if a committed showcase is wanted, add one new dedicated example
*with* its own compiled sidecar).

Note: repo-root `s.yaml` / `s.compiled.yaml` are the maintainer's untracked working
demo (`??` in `git status`). This batch does **not** commit them; the committed proof
of the features is the test suite and the docs. A committed demo would need its
`*.scenario.yaml` plus a freshly recompiled `*.compiled.yaml` added together.

## Testing

- `guidebot validate` accepts the updated `s.yaml` and any changed examples; the new
  config blocks parse.
- A smoke render of a minimal scenario with all toggles on succeeds and produces the
  expected audio track(s).
- `mkdocs build` still passes (docs parity) where trivially checkable.

## Recompile impact

Config toggles: none. The `s.yaml` popup-step change and any new `slide` step:
requires `guidebot compile`.
