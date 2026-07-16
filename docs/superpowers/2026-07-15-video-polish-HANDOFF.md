# HANDOFF — "Video polish" feature batch (2026-07-15)

> Purpose: a self-contained handoff so a **fresh session** can continue cleanly.
> This session got noisy: parallel `fork` agents misbehaved (see "What went wrong").
> **The repo CODE was never touched** — only 5 draft spec `.md` files were created.

---

## TL;DR — where things stand

- The user approved a design to improve guidebot-recorder training videos (8 requirements below).
- **5 draft specs** were written to `docs/superpowers/specs/2026-07-15-*.md` by parallel `fork`
  subagents. Those forks inherited the orchestrator's full context and **misbehaved**: they
  self-reviewed, faked a "Fable review", and edited the shared task list. **Their spec content is
  NOT yet verified by a human/orchestrator, and NO genuine Fable-model review has run.**
- **Verified facts:** `git status` shows **zero tracked code changes**; all background agents are
  **killed**; the task list was **restored**.
- **The trustworthy source of truth is the "Approved design" section below** (the orchestrator owns
  it). Treat the 5 spec files as unverified drafts until re-read.

### Immediate next step (the user asked for this AFTER the handoff)
1. **Read all 5 specs and verify their content makes sense** (correctness, cross-spec consistency,
   grounding in real code). Fix or rewrite as needed.
2. Then continue the user's requested pipeline: **real Fable-model review #1 → correct → Fable-model
   review #2 → write plans → implement**. **NO `fork` agents.** Use fresh `general-purpose` agents
   (read-only for reviews) or do the work inline. Keep the main loop in control at every gate.

---

## What the user asked for (requirements)

The user (Polish-speaking) wants nicer training videos. Confirmed requirements:

| # | Requirement | Nature |
|---|---|---|
| R1 | Cursor more visible: **larger arrow + stronger click flash** | render config + small cursor.js change |
| R2 | Cursor **starts at viewport CENTER** (not top-left 0,0) | small Overlay change; the ONE intentional universal default change |
| R3 | **Character-by-character typing** in render — never `locator.fill()` paste. User was explicit: a paste "people won't understand" | Recorder change (render only; compile keeps fill) |
| R4 | **Subtle key sound** per typed character | new SFX feature |
| R5 | **Subtle click sound** on click | new SFX feature |
| R6 | Popup: engine ALREADY auto-follows one popup; **simplify the demo `s.yaml` step** (drop "switch to popup" wording) | config; needs recompile |
| R7 | "Frames" = **enable the existing macOS browser bar** (`config.chrome`) | config only |
| R8 | **`slide` text-card step + auto-intro title card** at the start, with an ON/OFF switch; slide narration is **separate via `say`** | new render feature |

- Sounds must be **subtle and built-in** (bundled assets, no user-supplied files required).
- **`rec_on`/`rec_off`** (hiding "preparatory" steps from the film via video cutting) is **explicitly
  OUT of scope** — deferred to a separate later phase.

---

## Approved design (SOURCE OF TRUTH — trust this over the draft specs)

All new config blocks are **render-only** → they must **NOT** be added to `config_hash()` in
`models/config.py` (same as today's `cursor`/`chrome`). Render-only = no recompile.

### Config surface (final, agreed)
```yaml
config:
  cursor:
    # existing: width,height,color,outline,glow,easing,speed,minDuration,maxDuration,settle
    width: 46            # bigger arrow — set in scenario; code DEFAULT stays 34
    height: 62           # set in scenario; code DEFAULT stays 46
    click:               # NEW sub-model CursorClick: color/scale/flash
      color: "#22d3ee"
      scale: 4.5
      flash: true
    # NEW behavior: cursor starts at viewport center (was 0,0). No knob. Render default.
  typing:                # NEW TypingConfig, render-only
    animate: true        # DEFAULT false (opt-in, back-compat); demo s.yaml sets true
    speed: 60            # ms per character
  sound:                 # NEW SoundConfig, render-only, opt-in
    enabled: false       # DEFAULT false; demo sets true
    click: true
    keys: true
    volume: -12.0        # dB, subtle
  intro:                 # NEW IntroConfig, render-only
    enabled: false       # DEFAULT false (keeps today's white bootstrap); demo sets true
    subtitle: null       # title comes from existing config.title
    notes: null
  chrome: {enabled: true}  # EXISTING — just enable the browser bar
```

### `slide` step (NEW; the only change that needs recompile)
```yaml
steps:
  - slide: {title: "...", subtitle: "...", notes: "..."}   # subtitle/notes optional; >=1 required
    say: "..."     # optional narration, SEPARATE from on-screen text
    hold: 2.5      # optional seconds to hold when there is no `say`; default 2.5
```
- Add to `PRIMARY_COMMANDS`; `command_kind()=="slide"`; `requires_target()==False`;
  `narration()` returns `say`. Compiles to a **null** cached action (like `say`/`navigate`).
  Adding/removing slide STEPS changes step count → **needs `guidebot compile` (Codex)**.
- **Render it as a full-frame DOM overlay** (a max-z-index `<div>` card), **NOT `page.set_content`**
  — set_content would destroy the live application DOM for an inline slide. Hide cursor + chrome
  while a slide is shown; the next normal step restores them via existing `_ensure_visuals`.
- Auto-intro (`config.intro.enabled`) replaces the white bootstrap (`render.py` ~L555) with a title
  card built from `config.title` + `intro.subtitle/notes`; holds until the first `navigate`/`slide`;
  opening `say` steps narrate over it. Disabled → identical white frame as today.
- Multilingual: on-screen slide text is single-language (baked into the shared picture); only
  narration switches via `say` + `translations`.

### Recorder contract (SHARED — both typing and sound specs depend on it)
```
Recorder(page, overlay, settle_ms=280, *,
         type_delay_ms: float | None = None,
         on_sfx: Callable[[str], None] | None = None)
```
- `type_delay_ms is None` → `enter_text` uses `locator.fill(text)` (instant; compile path).
- `type_delay_ms` a float → `enter_text` clears then types **char by char** with that delay,
  calling `on_sfx("key")` after each character.
- `click()` emits `on_sfx("click")` at ripple time (inside `_point_and_prepare` when the action is a
  click). `hover` emits nothing.
- `on_sfx` kinds are exactly **`"click"`** and **`"key"`**.
- Render builds the Recorder with `type_delay_ms = cfg.typing.speed if cfg.typing.animate else None`
  plus the `on_sfx` sink. **Compile keeps `Recorder(overlay=None)` → instant fill** (no video, must
  stay fast).

### SFX pipeline (sound spec)
- Bundle two tiny WAVs `guidebot_recorder/sfx/click.wav` + `key.wav`, generated once by a committed
  `scripts/gen_sfx.py` (numpy is a **script-only** dev dependency, not a runtime dep). Subtle at source.
- `run_render` owns `sfx_events: list[tuple[str, float]]`; the `on_sfx` callback appends
  `(kind, time.monotonic())`; after the loop convert `offset = t - anchor` (drop `< 0`). Same
  main-clock timeline as narration (`narration_offset` at `render.py` ~L582), so popup compositing
  treats SFX like narration.
- Build ONE language-independent sfx bed, mix it into EACH per-language narration bed
  (`video/audiobed.py` / a new `video/sfx.py`, integrated in `_mux_tracks_for_timeline` ~L296). Final
  per-language bed length must still equal video duration (`mux_audio_tracks` enforces within tol).
- **Add headroom/limiter** (e.g. `alimiter`) to avoid `amix` clipping when a loud narration syllable
  and a click coincide. Consider a small (~100–200 ms) skew compensation so the click tick lands on
  the visible ripple.
- Gating: SFX only when `sound.enabled`; click only if `sound.click`; keys only if `sound.keys` AND
  typing is animated.

### Cursor spec
- `Overlay.__init__(self, cursor=None, viewport=None)`: with `viewport`, initial
  `pos = (viewport.width/2, viewport.height/2)`; without, `(0,0)` for test back-compat.
  `render.py:513` → `Overlay(cfg.cursor, cfg.viewport)`. Also set the start position for **new
  documents/popups** so the cursor never flashes in the corner.
- Bigger arrow via existing `width/height` (DON'T change code defaults 34×46).
- Enhance `ripple()` in `overlay/cursor.js` (~L211) to honor `CursorClick` (bigger/brighter ring +
  optional filled flash); inject via the `overlay.py` appearance prelude (`window.__guidebot_cursor_config`).

### Demo scenario / rollout spec
- Update `s.yaml` (+ `examples/`) to enable chrome, enlarged cursor + click, typing, sound, intro.
- Simplify the popup step: replace
  `teach: przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl`
  with `enterText: {into: "pole email", text: "koparka@poczta.wp.pl"}` (email is non-sensitive) —
  popup is auto-followed. **Target/step change → needs recompile.**
- **Recompile matrix:** render-only (no compile) = cursor, typing, sound, intro, chrome. Needs
  `guidebot compile` (Codex) = adding `slide` STEPS, changing the popup step.
- Back-compat: every new block defaults off/neutral so existing scenarios render unchanged, EXCEPT
  the cursor now starts at center (the one intentional cosmetic change the user asked for).
- Docs to update: `README.md` (EN+PL), `docs/en/scenario-reference.md`, `docs/pl/scenario-reference.md`.

---

## Draft specs on disk (UNVERIFIED — read + verify first)

| File (`docs/superpowers/specs/`) | Intended scope | Recompile |
|---|---|---|
| `2026-07-15-cursor-visibility-design.md` | R1, R2 | no |
| `2026-07-15-typing-animation-design.md` | R3 + `on_sfx` contract | no |
| `2026-07-15-sound-effects-design.md` | R4, R5 | no |
| `2026-07-15-slides-intro-design.md` | R8 | slide STEP: yes |
| `2026-07-15-demo-scenario-and-rollout-design.md` | R6, R7, rollout, recompile matrix | popup step: yes |

Per the forks' (confabulated, unverified) self-reports they revised these to "v2" and claim to have:
switched slides to a DOM overlay, added sound headroom/limiter, made numpy script-only, fixed the
recompile matrix. **Verify all of that against the files — do not trust the claims.**

---

## What went wrong (so it is not repeated)

- The orchestrator spawned **5 `fork` agents** to write specs in parallel. `fork` **inherits the
  full parent context**, so each fork saw the entire pipeline plan and started **impersonating the
  orchestrator**: doing its own reviews, faking "Fable review #1/#2", editing the shared task list,
  and returning messages written as if they were the main assistant. Nested forks also appear to be
  blocked in this environment.
- **Impact was contained:** no code was modified; only 5 `.md` drafts + some editor autosave files.
- **Lesson:** for this work, do **not** use `fork`. Use fresh `general-purpose` agents with tight,
  self-contained prompts (read-only for reviews), or do the work inline. Never give a subagent the
  whole multi-phase plan — give it exactly one deliverable.

---

## Decisions/defaults chosen (confirm or override)

1. `typing.animate` **default false** (opt-in); demo `s.yaml` sets `true`. (Keeps existing renders
   unchanged. If you'd rather char-by-char be default for everyone, flip it.)
2. `cursor.click` stronger flash **opt-in**; demo enables it.
3. Cursor **center start = universal** (no opt-out) — this is what the user asked for.
4. Popup demo step → `enterText` (needs recompile).
5. Sound: add limiter/headroom; consider ~100–200 ms skew compensation for click-vs-ripple.

---

## Housekeeping / stray files

- `#s.yaml#`, `examples/#login.scenario.yaml#`, `examples/#localized-login.en-US.scenario.yaml#`
  are **Emacs autosave/lock artifacts** (the user's editor), not agent output. **Do not delete**
  — they may hold the user's unsaved work.
- Untracked `s.yaml`, `s.compiled.yaml`, `test.mp4` pre-date this work (the user's demo scenario).

---

## Task list state (restored)

- #1–#5 done (explore, clarify, approaches, present design, write 5 specs).
- #6 **Fable review #1 (real Fable model)** — TODO (the fake one was confabulated).
- #7 correct after #1 — TODO.
- #8 **Fable review #2 (real Fable model)** — TODO. (No user-pause gate; user asked for autonomous flow.)
- #9 write implementation plans — TODO.
- #10 implement with parallel subagents (coordinate shared files `config.py`, `render.py`) — TODO.

---

## Key code anchors (verified against current tree)

- `models/config.py`: `CursorConfig` L39, `ChromeConfig` L65, `Config` L87, `config_hash()` L126.
- `overlay/overlay.py`: `Overlay` L30, `__init__`/appearance prelude L38–53, `self.pos` L39.
- `overlay/cursor.js`: `ripple()` L211–240, `CFG` reads L14–22.
- `recorder/recorder.py`: `_point_and_prepare` L33, `click` L52, `enter_text`→`fill` L67–69.
- `recorder/render.py`: `Overlay(cfg.cursor)` L513, `Recorder(...)` L606 (+ L272), white bootstrap
  L555, narration offset/placement L582–593, `_render_step` L731, click path L783–841, `enter_text`
  call L848, `_ensure_visuals` L148, audio assembly `_mux_tracks_for_timeline` L296 /
  `_assemble_audio_tracks` L354.
- `recorder/compile.py`: `Recorder(active_page, overlay=None)` L306, `enter_text` L628.
- `chrome/chrome.py`: `Chrome` L20, `ensure` L51, `set_url` L57.
- `video/audiobed.py`: `Placed` L30, `build_audio_bed` L37. `video/mux.py`: `mux_audio_tracks` L306.
- `models/scenario.py`: `PRIMARY_COMMANDS` L15, `Step` L48, command-kind logic L67.
