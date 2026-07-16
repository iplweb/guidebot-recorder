# Video Polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make guidebot-recorder training videos clearer — bigger/centered cursor with a stronger click flash, character-by-character typing, subtle key/click sounds, an enabled browser bar, and full-frame text slides with an auto-intro title card.

**Architecture:** Five render-time features layered onto the existing compile/render pipeline. All new config is **render-only** (never enters `config_hash()`), so nothing forces a recompile except adding a `slide` **step** or editing the demo's popup **step**. Features default OFF/neutral (existing renders stay byte-identical) except the cursor now starts centered. Heavy shared-file contention (`models/config.py`, `recorder/render.py`, `recorder/recorder.py`, `overlay/`) is serialized by the phase order below.

**Tech Stack:** Python 3, pydantic v2, Playwright (async, real Chromium in tests), ffmpeg/ffprobe, pytest (`asyncio_mode = "auto"`), hatchling packaging, numpy (script-only, via PEP 723).

**Source specs (all REVIEWED — GO, 2026-07-15):** `docs/superpowers/specs/2026-07-15-{cursor-visibility,typing-animation,sound-effects,slides-intro,demo-scenario-and-rollout}-design.md`. Each spec is normative for its own block; this plan sequences them.

## Global Constraints

- **Render-only config:** none of `CursorClick`, `TypingConfig`, `SoundConfig`, `IntroConfig` may be added to `config_hash()` (`models/config.py:126-139`, an allow-list of viewport/locale/tts_lang). Verify by test.
- **Defaults inert:** `typing.animate=False`, `sound.enabled=False`, `intro.enabled=False`; cursor code defaults stay `34×46`; `CursorClick` defaults reproduce today's ripple exactly (`color="rgba(37,99,235,.9)"`, `scale=3.25`, `flash=False`). Existing scenarios must render byte-identically — **except** the cursor now starts at viewport center (the one intended universal change).
- **Fail-loud:** no bare `except: pass`; no bare `assert` for runtime invariants (`python -O` strips them) — raise `RenderError`/`ValueError`. Every except logs, re-raises, or raises a different error.
- **Recompile matrix:** render-only (no compile) = cursor, typing, sound, intro, chrome. Needs `guidebot compile` (Codex) = adding/removing/reordering a `slide` STEP, or changing the demo popup step (`teach`→`enterText`).
- **`on_sfx` kinds are exactly** `"click"` and `"key"`.
- **TDD:** write the failing test first, watch it fail, minimal implementation, watch it pass, commit. Frequent commits. DRY, YAGNI.
- **Tests run:** `pytest` from repo root. Real-Chromium tests use the `async_playwright` fixture pattern in `tests/unit/overlay/test_overlay.py`. ffmpeg tests carry `@pytest.mark.ffmpeg`; network/TTS tests carry `@pytest.mark.network`.
- **Known pre-existing failures (NOT regressions):** `tests/unit/recorder/test_compile.py::test_popup_opened_during_reasoning_is_unexpected_and_click_is_not_run` and `::test_popup_opened_during_click_preparation_is_not_attributed` (substring redaction of "cli" in "click"). Do not treat as regressions.

## Phase order & dependencies

```
Phase 0  Model & config foundation          (disjoint; everything depends on it)
Phase 1  Overlay/Chrome JS+Py bundle         (depends: 0)  ← single owner; lands before any render card logic
Phase 2  Recorder: typing + on_sfx           (depends: 0,1)  ← calls overlay.ripple(flash=)
Phase 3  Render wiring: cursor center + typing (depends: 0,1,2)
Phase 4  Sound: assets + bed + render mix-in  (depends: 0,2,3)
Phase 5  Slides + intro                       (depends: 0,1,3)
Phase 6  Demo scenario + docs (+ recompile)   (depends: all)
```

Phases 4 and 5 are independent of each other and may run in parallel once Phase 3 lands. Within a phase, tasks are ordered.

---

## Phase 0 — Model & config foundation

Disjoint from the browser/render code; pure pydantic + validators. Establishes every new config surface so later phases can import them.

### Task 0.1: `CursorClick` sub-model + `CursorConfig.click`

**Files:**
- Modify: `guidebot_recorder/models/config.py` (add class before `CursorConfig`; add field to `CursorConfig`)
- Test: `tests/unit/models/test_config.py` (extend)

**Interfaces:**
- Produces: `class CursorClick(BaseModel)` with `color: str = "rgba(37,99,235,.9)"`, `scale: float = Field(default=3.25, gt=0)`, `flash: bool = False`; and `CursorConfig.click: CursorClick = Field(default_factory=CursorClick)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/models/test_config.py
from guidebot_recorder.models.config import CursorConfig, CursorClick

def test_cursor_click_defaults_match_todays_ripple():
    c = CursorConfig()
    assert c.click.color == "rgba(37,99,235,.9)"
    assert c.click.scale == 3.25
    assert c.click.flash is False

def test_cursor_click_rejects_unknown_keys_and_nonpositive_scale():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CursorClick(bogus=1)
    with pytest.raises(ValidationError):
        CursorClick(scale=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_config.py -k cursor_click -v`
Expected: FAIL (`ImportError: cannot import name 'CursorClick'`).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/models/config.py — add above class CursorConfig
class CursorClick(BaseModel):
    """Appearance of the click ripple. Defaults reproduce today's hard-coded ring."""

    model_config = ConfigDict(extra="forbid")
    color: str = "rgba(37,99,235,.9)"          # today's ring colour (cursor.js:227)
    scale: float = Field(default=3.25, gt=0)   # today's end-scale (cursor.js:234); > 0
    flash: bool = False                        # opt-in filled disc under the ring
```

Then add to `CursorConfig` (after the motion-timing fields):

```python
    # --- Click ripple appearance (render-only; injected into cursor.js) ---
    click: CursorClick = Field(default_factory=CursorClick)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/models/test_config.py -k cursor_click -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/models/config.py tests/unit/models/test_config.py
git commit -m "feat(config): add CursorClick ripple appearance sub-model"
```

### Task 0.2: `TypingConfig` + `Config.typing`

**Files:**
- Modify: `guidebot_recorder/models/config.py`
- Test: `tests/unit/models/test_config.py`

**Interfaces:**
- Produces: `class TypingConfig(BaseModel)` with `animate: bool = False`, `speed: int = Field(default=60, gt=0)`; and `Config.typing: TypingConfig = Field(default_factory=TypingConfig)`.

- [ ] **Step 1: Write the failing test**

```python
def test_typing_config_defaults_and_bounds():
    from guidebot_recorder.models.config import TypingConfig
    import pytest
    from pydantic import ValidationError
    t = TypingConfig()
    assert t.animate is False and t.speed == 60
    with pytest.raises(ValidationError):
        TypingConfig(speed=0)
    with pytest.raises(ValidationError):
        TypingConfig(bogus=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_config.py -k typing_config -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/models/config.py
class TypingConfig(BaseModel):
    """Render-only character-by-character input animation."""

    model_config = ConfigDict(extra="forbid")
    animate: bool = False                  # opt-in; keeps existing renders inert
    # ms PER CHARACTER — a *delay* (higher = slower). NOT CursorConfig.speed, which is
    # a px/ms *rate* (higher = faster). Same word, inverted meaning; do not confuse.
    speed: int = Field(default=60, gt=0)
```

Add to `Config`:

```python
    typing: TypingConfig = Field(default_factory=TypingConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/models/test_config.py -k typing_config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/models/config.py tests/unit/models/test_config.py
git commit -m "feat(config): add TypingConfig (render-only typing animation)"
```

### Task 0.3: `SoundConfig` + `Config.sound`

**Files:**
- Modify: `guidebot_recorder/models/config.py`
- Test: `tests/unit/models/test_config.py`

**Interfaces:**
- Produces: `class SoundConfig(BaseModel)` with `enabled: bool = False`, `click: bool = True`, `keys: bool = True`, `volume: float = Field(default=-12.0, le=0)`; and `Config.sound: SoundConfig = Field(default_factory=SoundConfig)`.

- [ ] **Step 1: Write the failing test**

```python
def test_sound_config_defaults_and_bounds():
    from guidebot_recorder.models.config import SoundConfig
    import pytest
    from pydantic import ValidationError
    s = SoundConfig()
    assert (s.enabled, s.click, s.keys, s.volume) == (False, True, True, -12.0)
    with pytest.raises(ValidationError):
        SoundConfig(volume=3.0)   # positive gain rejected (le=0)
    with pytest.raises(ValidationError):
        SoundConfig(bogus=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_config.py -k sound_config -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/models/config.py
class SoundConfig(BaseModel):
    """Render-only, opt-in built-in SFX mixed under the narration."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    click: bool = True
    keys: bool = True
    # dB attenuation on the SFX bed; <= 0 only. A positive gain would erode the
    # −20 dBFS source headroom the clipping defence relies on.
    volume: float = Field(default=-12.0, le=0)
```

Add to `Config`:

```python
    sound: SoundConfig = Field(default_factory=SoundConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/models/test_config.py -k sound_config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/models/config.py tests/unit/models/test_config.py
git commit -m "feat(config): add SoundConfig (render-only SFX)"
```

### Task 0.4: `IntroConfig` + `Config.intro`

**Files:**
- Modify: `guidebot_recorder/models/config.py`
- Test: `tests/unit/models/test_config.py`

**Interfaces:**
- Produces: `class IntroConfig(BaseModel)` with `enabled: bool = False`, `subtitle: str | None = None`, `notes: str | None = None`; and `Config.intro: IntroConfig = Field(default_factory=IntroConfig)`.

- [ ] **Step 1: Write the failing test**

```python
def test_intro_config_defaults():
    from guidebot_recorder.models.config import IntroConfig
    i = IntroConfig()
    assert i.enabled is False and i.subtitle is None and i.notes is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_config.py -k intro_config -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/models/config.py
class IntroConfig(BaseModel):
    """Render-only auto-intro title card (replaces the white bootstrap when enabled)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    subtitle: str | None = None
    notes: str | None = None
```

Add to `Config`:

```python
    intro: IntroConfig = Field(default_factory=IntroConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/models/test_config.py -k intro_config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/models/config.py tests/unit/models/test_config.py
git commit -m "feat(config): add IntroConfig (render-only intro title card)"
```

### Task 0.5: `config_hash()` isolation regression test

**Files:**
- Test: `tests/unit/models/test_config.py`

- [ ] **Step 1: Write the failing test** (it should pass immediately — this is a guard proving the render-only invariant; if it fails, a prior task wrongly touched `config_hash`)

```python
def test_new_render_only_blocks_do_not_change_config_hash():
    from guidebot_recorder.models.config import (
        Config, Viewport, TtsConfig, CursorConfig, CursorClick,
        TypingConfig, SoundConfig, IntroConfig,
    )
    from guidebot_recorder.models.config import config_hash
    base = Config(
        title="t", viewport=Viewport(width=800, height=600),
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
    )
    h0 = config_hash(base)
    mutated = base.model_copy(update={
        "cursor": CursorConfig(click=CursorClick(flash=True, scale=4.5)),
        "typing": TypingConfig(animate=True, speed=40),
        "sound": SoundConfig(enabled=True, volume=-6.0),
        "intro": IntroConfig(enabled=True, subtitle="s"),
    })
    assert config_hash(mutated) == h0
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/models/test_config.py -k config_hash -v`
Expected: PASS (the blocks are outside the hash projection by construction).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/models/test_config.py
git commit -m "test(config): guard that render-only blocks stay out of config_hash"
```

### Task 0.6: `Slide` model + `slide` step wiring in `scenario.py`

**Files:**
- Modify: `guidebot_recorder/models/scenario.py` (add `Slide`; add `slide` to `PRIMARY_COMMANDS` and `Step`; extend `command_kind`)
- Test: `tests/unit/models/test_scenario.py` (or `tests/unit/scenario/`; match existing location for scenario/step tests)

**Interfaces:**
- Produces: `class Slide(BaseModel)` `{title: str|None=None, subtitle: str|None=None, notes: str|None=None, hold: float = 2.5}` with `extra="forbid"` and a validator requiring ≥1 of title/subtitle/notes; `Step.slide: Slide | None = None`; `"slide"` in `PRIMARY_COMMANDS`; `Step.command_kind()` returns `"slide"`; `requires_target()` returns `False` for slide; `narration()` returns the step's `say`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/models/test_scenario.py
import pytest
from pydantic import ValidationError
from guidebot_recorder.models.scenario import Step, Slide

def test_slide_requires_at_least_one_text_field():
    with pytest.raises(ValidationError):
        Slide()
    s = Slide(title="Logowanie")
    assert s.hold == 2.5

def test_step_slide_command_kind_and_no_target():
    step = Step(slide=Slide(title="T"), say="narracja")
    assert step.command_kind() == "slide"
    assert step.requires_target() is False
    assert step.narration() == "narracja"

def test_slide_is_mutually_exclusive_with_other_primaries():
    with pytest.raises(ValidationError):
        Step(slide=Slide(title="T"), click="ok")

def test_silent_slide_forbids_translations_say_slide_requires_them():
    # silent slide: narration() is None → translations must be empty
    Step(slide=Slide(title="T"))  # ok, no translations
    with pytest.raises(ValidationError):
        # a say-less slide with translations is rejected by the scenario validator;
        # tested at Scenario level in test_scenario_translations (below/existing).
        from guidebot_recorder.models.scenario import Scenario
        from guidebot_recorder.models.config import Config, Viewport, TtsConfig
        Scenario(
            config=Config(title="t", viewport=Viewport(width=8, height=6),
                          tts=TtsConfig(provider="edge", voice="v", lang="pl-PL")),
            steps=[Step(slide=Slide(title="T"), translations={"en-US": "x"})],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_scenario.py -k slide -v`
Expected: FAIL (ImportError `Slide`).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/models/scenario.py

PRIMARY_COMMANDS = ("teach", "navigate", "click", "hover", "enter_text", "wait", "slide")


class Slide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    subtitle: str | None = None
    notes: str | None = None
    hold: float = 2.5

    @model_validator(mode="after")
    def _at_least_one_text(self) -> "Slide":
        if not any((self.title, self.subtitle, self.notes)):
            raise ValueError("slide wymaga co najmniej jednego z: title/subtitle/notes")
        return self
```

Add the field to `Step` (near the other primaries):

```python
    slide: Slide | None = None
```

`command_kind()` already iterates `PRIMARY_COMMANDS` via `getattr`; with `"slide"` in the tuple it returns `"slide"` automatically (no special-casing needed — unlike `enter_text`→`enterText`). `requires_target()` returns `False` for `"slide"` (falls through its `if` branches). `narration()` already returns `say` first, so a slide-with-say narrates and a silent slide returns `None` — no change. `_exactly_one_command` and `Scenario._complete_audio_translations` need no change (they key off `PRIMARY_COMMANDS` / `narration()`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/models/test_scenario.py -k slide -v`
Expected: PASS.

- [ ] **Step 5: Run the full model suite (no regressions)**

Run: `pytest tests/unit/models -v`
Expected: PASS (all existing step/scenario/config tests still green).

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/models/scenario.py tests/unit/models/test_scenario.py
git commit -m "feat(scenario): add slide step + Slide model (null-compiled, render-only text card)"
```

---

## Phase 1 — Overlay / Chrome JS+Python bundle (single owner)

This whole phase is ONE bundle owned by a single implementer: the cursor spec's `ripple(flash)` + `CFG.start` + `viewport` centering **and** the slides spec's `hide`/`show` + persistent `hidden` flag on both `overlay` and `chrome`. It must land before any render-side card logic (Phase 5). Tests use real Chromium (`tests/unit/overlay/`, `tests/unit/chrome/`).

### Task 1.1: `cursor.js` — configurable ripple + optional flash + `CFG.start` seed + persistent `hidden` flag

**Files:**
- Modify: `guidebot_recorder/overlay/cursor.js`
- Test: `tests/unit/overlay/test_cursor_js.py` (new; or extend `test_overlay.py`)

**Interfaces:**
- Produces (JS API on `window.__guidebot_cursor`): `ripple(flash)` honors `CFG.click.{color,scale,flash}`; `hide()` sets a persistent `hidden` flag + `display:none`; `show()` clears it + restores; `ensure()`/`styleCursor` HONOR the flag (stay `display:none` while hidden). `state.x/state.y` seed from `CFG.start` (else `0`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/overlay/test_cursor_js.py
import json
import pytest
from collections.abc import AsyncIterator
from playwright.async_api import Page, async_playwright

@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page()
        try:
            yield pg
        finally:
            await b.close()

CURSOR_JS = (__import__("importlib.resources", fromlist=["files"])
             .files("guidebot_recorder.overlay").joinpath("cursor.js").read_text("utf-8"))

async def _inject(page: Page, cfg: dict) -> None:
    await page.evaluate(f"window.__guidebot_cursor_config = {json.dumps(cfg)};")
    await page.evaluate(CURSOR_JS)

async def test_start_seed_centers_first_mount(page: Page):
    await page.set_content("<div></div>")
    await _inject(page, {"start": [400, 300]})
    pos = await page.evaluate("window.__guidebot_cursor.position")
    assert pos == [400, 300]

async def test_ripple_flash_draws_filled_disc_only_when_configured_and_requested(page: Page):
    await page.set_content("<div></div>")
    await _inject(page, {"click": {"color": "rgb(1,2,3)", "scale": 5, "flash": True}})
    # ripple(true) synchronously creates the ring (+ flash disc); read immediately.
    n = await page.evaluate(
        "() => { window.__guidebot_cursor.ripple(true);"
        " return document.querySelectorAll('[data-guidebot-ripple],[data-guidebot-flash]').length; }"
    )
    assert n >= 2  # ring + flash
    # flash=false → ring only
    n2 = await page.evaluate(
        "() => { document.querySelectorAll('[data-guidebot-flash]').forEach(e=>e.remove());"
        " window.__guidebot_cursor.ripple(false);"
        " return document.querySelectorAll('[data-guidebot-flash]').length; }"
    )
    assert n2 == 0

async def test_hidden_flag_survives_ensure(page: Page):
    await page.set_content("<div></div>")
    await _inject(page, {})
    await page.evaluate("window.__guidebot_cursor.hide()")
    await page.evaluate("window.__guidebot_cursor.ensure()")
    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp == "none"
    await page.evaluate("window.__guidebot_cursor.show()")
    disp2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp2 == "block"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/overlay/test_cursor_js.py -v`
Expected: FAIL (`CFG.start` unused → position `[0,0]`; no `hide`/`show`; ripple takes no arg / no flash element).

- [ ] **Step 3: Write minimal implementation** (edit `cursor.js`)

  1. State seed (cursor.js ~L39-42): replace the `0` fallbacks with `CFG.start`:
     ```js
     const START = Array.isArray(CFG.start) ? CFG.start : [0, 0];
     const state = {
       x: Number.isFinite(initialX) ? initialX : (Number(START[0]) || 0),
       y: Number.isFinite(initialY) ? initialY : (Number(START[1]) || 0),
     };
     let hidden = false;   // persistent suppression flag (survives ensure())
     ```
  2. `styleCursor` (cursor.js:113): make the `display` honor the flag:
     ```js
     setImportant(cursor, "display", hidden ? "none" : "block");
     ```
  3. `ripple(flash)` (cursor.js:211): read `CFG.click` and, when `flash` **and** `CFG.click.flash`, draw a second filled disc:
     ```js
     function ripple(flash = false) {
       if (!ensure()) return false;
       const root = mountRoot();
       if (!root) return false;
       const click = CFG.click || {};
       const ringColor = click.color || "rgba(37, 99, 235, .9)";
       const endScale = Number.isFinite(Number(click.scale)) ? Number(click.scale) : 3.25;
       const ring = document.createElement("div");
       ring.setAttribute("data-guidebot-ripple", "");
       styleTransient(ring, "2147483646");
       setImportant(ring, "left", `${state.x - 8}px`);
       setImportant(ring, "top", `${state.y - 8}px`);
       setImportant(ring, "width", "16px");
       setImportant(ring, "height", "16px");
       setImportant(ring, "border", `3px solid ${ringColor}`);
       setImportant(ring, "border-radius", "9999px");
       root.appendChild(ring);
       const anim = ring.animate(
         [{ opacity: 0.95, transform: "scale(.35)" },
          { opacity: 0, transform: `scale(${endScale})` }],
         { duration: 500, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
       removeAfterAnimation(ring, anim, 600);
       if (flash && click.flash) {
         const disc = document.createElement("div");
         disc.setAttribute("data-guidebot-flash", "");
         styleTransient(disc, "2147483645");
         setImportant(disc, "left", `${state.x - 8}px`);
         setImportant(disc, "top", `${state.y - 8}px`);
         setImportant(disc, "width", "16px");
         setImportant(disc, "height", "16px");
         setImportant(disc, "background", ringColor);
         setImportant(disc, "border-radius", "9999px");
         root.appendChild(disc);
         const fa = disc.animate(
           [{ opacity: 0.55, transform: "scale(.2)" },
            { opacity: 0, transform: "scale(2)" }],
           { duration: 420, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
         removeAfterAnimation(disc, fa, 520);
       }
       return true;
     }
     ```
  4. Add `hide`/`show` and expose them on the API object (near the `api` literal ~L278):
     ```js
     function hide() { hidden = true; const c = document.querySelector(CURSOR_SELECTOR); if (c) setImportant(c, "display", "none"); }
     function show() { hidden = false; ensure(); }
     ```
     and add `hide, show` to the `api` object.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/overlay/test_cursor_js.py -v`
Expected: PASS.

- [ ] **Step 5: Full overlay suite (no regressions)**

Run: `pytest tests/unit/overlay -v`
Expected: PASS (existing ripple test still green — `ripple()` with no arg defaults `flash=false`, identical ring).

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/overlay/cursor.js tests/unit/overlay/test_cursor_js.py
git commit -m "feat(overlay/js): configurable ripple + optional flash, CFG.start seed, persistent hidden flag"
```

### Task 1.2: `overlay.py` — `viewport` centering, prelude `click`+`start`, `ripple(flash=)`, `hide`/`show`

**Files:**
- Modify: `guidebot_recorder/overlay/overlay.py`
- Test: `tests/unit/overlay/test_overlay.py` (extend)

**Interfaces:**
- Consumes: cursor.js API from Task 1.1.
- Produces: `Overlay.__init__(self, cursor: CursorConfig | None = None, viewport: Viewport | None = None)` → `self.pos = (w/2, h/2)` when `viewport` given else `(0.0, 0.0)`; prelude carries `click` and (when viewport) `start`; `Overlay.ripple(self, page, *, flash: bool = False)`; `Overlay.hide(self, page)` / `Overlay.show(self, page)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/overlay/test_overlay.py — additions
from guidebot_recorder.models.config import Viewport, CursorConfig, CursorClick

def test_overlay_viewport_centers_pos_backcompat_zero():
    from guidebot_recorder.overlay.overlay import Overlay
    assert Overlay().pos == (0.0, 0.0)
    assert Overlay(CursorConfig()).pos == (0.0, 0.0)
    o = Overlay(CursorConfig(), Viewport(width=1000, height=400))
    assert o.pos == (500.0, 200.0)

def test_prelude_carries_click_and_start():
    import json, re
    from guidebot_recorder.overlay.overlay import Overlay
    o = Overlay(CursorConfig(click=CursorClick(flash=True)), Viewport(width=800, height=600))
    prelude = o._script.split("\n", 1)[0]
    cfg = json.loads(re.search(r"= (\{.*\});", prelude).group(1))
    assert cfg["click"]["flash"] is True
    assert cfg["start"] == [400.0, 300.0]

async def test_hide_show_and_ripple_flash(page):
    from guidebot_recorder.overlay.overlay import Overlay
    o = Overlay(CursorConfig(click=CursorClick(flash=True)), Viewport(width=800, height=600))
    await o.install(page)
    await o.hide(page)
    disp = await page.evaluate("getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display")
    assert disp == "none"
    await o.show(page)
    await o.ripple(page, flash=True)  # must not raise; TypeError would fail the test
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/overlay/test_overlay.py -k "viewport or prelude or hide_show" -v`
Expected: FAIL (`__init__` takes no `viewport`; no `start`; `ripple()` takes no `flash`; no `hide`).

- [ ] **Step 3: Write minimal implementation** (`overlay.py`)

```python
from guidebot_recorder.models.config import CursorConfig, Viewport  # add Viewport

class Overlay:
    def __init__(self, cursor: CursorConfig | None = None, viewport: Viewport | None = None) -> None:
        self.cursor = cursor or CursorConfig()
        if viewport is not None:
            self.pos = (viewport.width / 2, viewport.height / 2)
        else:
            self.pos = (0.0, 0.0)
        body = files("guidebot_recorder.overlay").joinpath("cursor.js").read_text(encoding="utf-8")
        appearance = {
            "width": self.cursor.width, "height": self.cursor.height,
            "fill": self.cursor.color, "stroke": self.cursor.outline,
            "glow": self.cursor.glow, "easing": self.cursor.easing,
            "click": {
                "color": self.cursor.click.color,
                "scale": self.cursor.click.scale,
                "flash": self.cursor.click.flash,
            },
        }
        if viewport is not None:
            appearance["start"] = [self.pos[0], self.pos[1]]
        prelude = f"window.__guidebot_cursor_config = {json.dumps(appearance)};\n"
        self._script = prelude + body

    async def ripple(self, page: Page, *, flash: bool = False) -> None:
        await self.ensure(page)
        await page.evaluate("(f) => window.__guidebot_cursor.ripple(f)", flash)

    async def hide(self, page: Page) -> None:
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_cursor.hide()")

    async def show(self, page: Page) -> None:
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_cursor.show()")
```

(Leave `_glide_duration`, `install`, `ensure`, `move_to`, `_restore_position` unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/overlay/test_overlay.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/overlay/overlay.py tests/unit/overlay/test_overlay.py
git commit -m "feat(overlay): viewport-centered pos, click+start prelude, ripple(flash=), hide/show"
```

### Task 1.3: `chrome.js` + `chrome.py` — persistent `hidden` flag + `hide`/`show`

**Files:**
- Modify: `guidebot_recorder/chrome/chrome.js`, `guidebot_recorder/chrome/chrome.py`
- Test: `tests/unit/chrome/test_chrome.py` (extend)

**Interfaces:**
- Produces (JS `window.__guidebot_chrome`): `hide()`/`show()` with a persistent `hidden` flag honored by `ensure()` (the bar stays `display:none` while hidden). Python: `Chrome.hide(self, page)` / `Chrome.show(self, page)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/chrome/test_chrome.py — additions
async def test_chrome_hidden_flag_survives_ensure(page):
    from guidebot_recorder.chrome.chrome import Chrome
    from guidebot_recorder.models.config import ChromeConfig
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)
    await chrome.hide(page)
    await chrome.ensure(page)
    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-chrome]')).display")
    assert disp == "none"
    await chrome.show(page)
    disp2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-chrome]')).display")
    assert disp2 != "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/chrome/test_chrome.py -k hidden_flag -v`
Expected: FAIL (`Chrome` has no `hide`).

- [ ] **Step 3: Write minimal implementation**

In `chrome.js`: add a closure `let hidden = false;`, make the bar-mounting `ensure()`/style path set `display` to `hidden ? "none" : <normal>`, and add `hide()`/`show()` (set/clear `hidden`, then re-apply) to the API object. Mirror the cursor.js pattern; keep `API_VERSION`.

In `chrome.py`:

```python
    async def hide(self, page: Page) -> None:
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_chrome.hide()")

    async def show(self, page: Page) -> None:
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_chrome.show()")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/chrome/test_chrome.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/chrome/chrome.js guidebot_recorder/chrome/chrome.py tests/unit/chrome/test_chrome.py
git commit -m "feat(chrome): persistent hidden flag + hide/show (for slide cards)"
```

---

## Phase 2 — Recorder: typing animation + `on_sfx`

**Files:**
- Modify: `guidebot_recorder/recorder/recorder.py`
- Test: `tests/unit/recorder/test_recorder.py` (extend)

**Interfaces:**
- Consumes: `Overlay.ripple(page, *, flash=...)` (Phase 1.2).
- Produces: `Recorder.__init__(self, page, overlay, settle_ms=280, *, type_delay_ms: float | None = None, on_sfx: Callable[[str], None] | None = None)`; `Recorder._point_and_prepare(self, target, *, click_sound: bool = False)`; `click()` passes `click_sound=True`; animated `enter_text` (char-by-char, `on_sfx("key")` per char, value-correction, contenteditable/control-char guards); click emits `on_sfx("click")` **at ripple time**, exactly once.

### Task 2.1: Constructor params + `click_sound` threading + click sound at ripple time

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/recorder/test_recorder.py — additions
# (reuse this module's existing `page` fixture; targets are built directly, matching
#  the existing tests: RoleTarget(role=..., name=...) / TestidTarget(testid=...))
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.recorder.recorder import Recorder

async def test_click_emits_exactly_one_click_sound_even_without_overlay(page):
    await page.set_content('<button>ok</button>')
    events = []
    rec = Recorder(page, None, on_sfx=events.append)   # overlay=None → fallback path
    await rec.click(RoleTarget(role="button", name="ok"))
    assert events == ["click"]

async def test_click_emits_one_click_sound_with_overlay(page):
    from guidebot_recorder.overlay.overlay import Overlay
    await page.set_content('<button>ok</button>')
    events = []
    overlay = Overlay()
    await overlay.install(page)
    rec = Recorder(page, overlay, on_sfx=events.append)
    await rec.click(RoleTarget(role="button", name="ok"))
    assert events == ["click"]

async def test_hover_emits_no_click_sound(page):
    await page.set_content('<button>ok</button>')
    events = []
    rec = Recorder(page, None, on_sfx=events.append)
    await rec.hover(RoleTarget(role="button", name="ok"))
    assert "click" not in events
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/recorder/test_recorder.py -k click_sound -v`
Expected: FAIL (`Recorder.__init__` takes no `on_sfx`).

- [ ] **Step 3: Write minimal implementation** (`recorder.py`)

```python
from collections.abc import Callable
from playwright.async_api import Error as PlaywrightError, Locator, Page  # add PlaywrightError

class Recorder:
    def __init__(self, page: Page, overlay: Overlay | None, settle_ms: float = 280, *,
                 type_delay_ms: float | None = None,
                 on_sfx: Callable[[str], None] | None = None) -> None:
        self.page = page
        self.overlay = overlay
        self.settle_ms = settle_ms
        self._type_delay_ms = type_delay_ms
        self._on_sfx = on_sfx

    async def _point_and_prepare(self, target: Target, *, click_sound: bool = False) -> Locator:
        locator = await build_locator(self.page, target)
        await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        rippled = False
        if self.overlay is not None:
            box = await locator.bounding_box()
            if box is not None:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await self.overlay.move_to(self.page, cx, cy)
                await self.overlay.ripple(self.page, flash=click_sound)
                if click_sound and self._on_sfx is not None:
                    self._on_sfx("click")     # AT ripple time, before the settle pause
                rippled = True
                await self.page.wait_for_timeout(self.settle_ms)
        if click_sound and not rippled and self._on_sfx is not None:
            self._on_sfx("click")             # fallback: no overlay / no bbox
        return locator

    async def click(self, target: Target, *, before_click=None) -> None:
        locator = await self._point_and_prepare(target, click_sound=True)
        if before_click is not None:
            before_click()
        await locator.click()

    async def hover(self, target: Target) -> None:
        locator = await self._point_and_prepare(target)
        await locator.hover()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/recorder/test_recorder.py -k click_sound -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/recorder/recorder.py tests/unit/recorder/test_recorder.py
git commit -m "feat(recorder): on_sfx + click_sound; emit click sound at ripple time"
```

### Task 2.2: Animated `enter_text` (char-by-char, key sounds, value-correction, guards)

- [ ] **Step 1: Write the failing test**

```python
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.recorder.recorder import Recorder

async def test_enter_text_instant_when_no_delay(page):
    await page.set_content('<label for="i">E</label><input id="i">')
    events = []
    rec = Recorder(page, None, on_sfx=events.append)  # type_delay_ms=None
    await rec.enter_text(RoleTarget(role="textbox", name="E"), "abc")
    assert await page.locator("#i").input_value() == "abc"
    assert events == []  # no key events on instant path

async def test_enter_text_animated_types_char_by_char_and_emits_keys(page):
    await page.set_content('<label for="i">E</label><input id="i">')
    events = []
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=events.append)
    await rec.enter_text(RoleTarget(role="textbox", name="E"), "hi!")
    assert await page.locator("#i").input_value() == "hi!"
    assert events == ["key", "key", "key"]  # exactly len(text)

async def test_enter_text_contenteditable_does_not_crash(page):
    await page.set_content('<div data-testid="d" contenteditable="true"></div>')
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=lambda k: None)
    await rec.enter_text(TestidTarget(testid="d"), "xy")  # input_value() raises → guarded fill
    assert await page.locator('[data-testid="d"]').text_content() == "xy"

async def test_enter_text_control_char_falls_back_to_instant(page):
    await page.set_content('<label for="t">T</label><textarea id="t"></textarea>')
    events = []
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=events.append)
    await rec.enter_text(RoleTarget(role="textbox", name="T"), "a\nb")
    assert await page.locator("#t").input_value() == "a\nb"
    assert events == []   # instant path, no per-char key events
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/recorder/test_recorder.py -k enter_text -v`
Expected: FAIL (animated path/guards not implemented; existing `test_enter_text_fills` still passes).

- [ ] **Step 3: Write minimal implementation**

```python
    async def enter_text(self, target: Target, text: str) -> None:
        locator = await self._point_and_prepare(target)   # no click sound, no flash
        if self._type_delay_ms is None or any(c in text for c in "\n\r\t"):
            await locator.fill(text)
            return
        await locator.fill("")
        for i, ch in enumerate(text):
            await locator.press_sequentially(ch)
            if self._on_sfx is not None:
                self._on_sfx("key")
            if i < len(text) - 1:
                await self.page.wait_for_timeout(self._type_delay_ms)
        try:
            needs_fix = await locator.input_value() != text
        except PlaywrightError:
            needs_fix = True   # non-input target (e.g. contenteditable): re-issue fill()
        if needs_fix:
            await locator.fill(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/recorder/test_recorder.py -k enter_text -v`
Expected: PASS.

- [ ] **Step 5: Full recorder suite (no regressions)**

Run: `pytest tests/unit/recorder/test_recorder.py -v`
Expected: PASS (existing `test_enter_text_fills` green — instant path unchanged when `type_delay_ms=None`).

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/recorder/recorder.py tests/unit/recorder/test_recorder.py
git commit -m "feat(recorder): animated char-by-char enter_text with key sounds + guards"
```

---

## Phase 3 — Render wiring: cursor center + typing

**Files:**
- Modify: `guidebot_recorder/recorder/render.py`
- Test: `tests/unit/recorder/test_render.py` (extend; may need `@pytest.mark.network`/`ffmpeg` for full renders — prefer targeted assertions)

**Interfaces:**
- Consumes: `Overlay(cursor, viewport)` (1.2), `Recorder(..., type_delay_ms=, on_sfx=)` (2.x).
- Produces: `run_render` builds `Overlay(cfg.cursor, cfg.viewport)` at render.py:513; the per-step Recorder (render.py:606) receives `type_delay_ms = cfg.typing.speed if cfg.typing.animate else None`. (The `on_sfx` sink is added in Phase 4.)

### Task 3.1: Center the cursor + wire typing into the render Recorder

- [ ] **Step 1: Write the failing test** (unit-level, targeted; drives a minimal scenario and asserts the Overlay is centered and the Recorder animates)

```python
# tests/unit/recorder/test_render.py — additions
def test_overlay_constructed_with_viewport(monkeypatch):
    # Assert render.py builds Overlay(cfg.cursor, cfg.viewport) — capture the args.
    import guidebot_recorder.recorder.render as R
    captured = {}
    class Spy(R.Overlay):
        def __init__(self, cursor=None, viewport=None):
            captured["viewport"] = viewport
            super().__init__(cursor, viewport)
    monkeypatch.setattr(R, "Overlay", Spy)
    # ... run_render against a tiny compiled fixture with viewport 640x480 ...
    # assert captured["viewport"].width == 640
```

(Use the existing render test harness/fixtures in `test_render.py`; if a full render is too heavy for unit scope, assert via the `Spy` capture without completing the whole render, or mark `@pytest.mark.network`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/recorder/test_render.py -k overlay_constructed_with_viewport -v`
Expected: FAIL (`captured["viewport"]` is `None` — today `Overlay(cfg.cursor)`).

- [ ] **Step 3: Write minimal implementation** (`render.py`)

- Line 513: `overlay = Overlay(cfg.cursor, cfg.viewport)`.
- Line ~606: build the per-step Recorder with typing:
  ```python
  recorder = Recorder(
      active_page, overlay, settle_ms=cfg.cursor.settle,
      type_delay_ms=(cfg.typing.speed if cfg.typing.animate else None),
      # on_sfx sink added in Phase 4; None for now
  )
  ```
- Do **not** touch the readiness-only Recorder at render.py:272 (stays instant).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/recorder/test_render.py -k overlay_constructed_with_viewport -v`
Expected: PASS.

- [ ] **Step 5: Regression — existing render tests + a real smoke render**

Run: `pytest tests/unit/recorder -v` (and, where ffmpeg/network available, one end-to-end render of an existing example) 
Expected: PASS; an existing scenario renders unchanged except the cursor now starts centered.

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): center cursor at viewport start; wire typing animation into render Recorder"
```

---

## Phase 4 — Sound: assets + bed + render mix-in

Depends on Phase 2 (`on_sfx`) and Phase 3 (Recorder wiring). New leaf files (`sfx/`, `scripts/gen_sfx.py`, `video/sfx.py`) can be built before the render integration.

### Task 4.1: `scripts/gen_sfx.py` — deterministic WAV generator (numpy, PEP 723)

**Files:**
- Create: `scripts/gen_sfx.py`
- Create: `guidebot_recorder/sfx/__init__.py` (empty; makes `guidebot_recorder.sfx` importable)
- Create (generated, committed): `guidebot_recorder/sfx/click.wav`, `guidebot_recorder/sfx/key.wav`
- Test: `tests/unit/video/test_gen_sfx.py`

**Interfaces:**
- Produces: two mono 48 kHz 16-bit WAVs, peak-limited ≤ −20 dBFS, byte-identical across runs (fixed RNG seed). `key.wav` ≈25 ms; `click.wav` ≈60 ms.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/video/test_gen_sfx.py
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

def test_gen_sfx_is_byte_deterministic(tmp_path):
    np = pytest.importorskip("numpy")
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_sfx", "scripts/gen_sfx.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    a = tmp_path / "a"; b = tmp_path / "b"
    mod.generate(a); mod.generate(b)
    assert (a / "click.wav").read_bytes() == (b / "click.wav").read_bytes()
    assert (a / "key.wav").read_bytes() == (b / "key.wav").read_bytes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/video/test_gen_sfx.py -v`
Expected: FAIL (no assets, no `scripts/gen_sfx.py`).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/gen_sfx.py
# /// script
# dependencies = ["numpy"]
# ///
"""Deterministic generator for the two bundled SFX WAVs. Run: uv run scripts/gen_sfx.py
Regenerates guidebot_recorder/sfx/{click,key}.wav. Fixed RNG seed → byte-identical."""
from __future__ import annotations
import wave, struct
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
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(struct.pack(f"<{len(data)}h", *data.tolist()))

def _key(rng) -> np.ndarray:
    n = int(SR * 0.025)                      # ~25 ms
    t = np.arange(n) / SR
    env = np.exp(-t / 0.006)                 # fast decay (~1 ms attack ignored, ~20 ms tail)
    noise = rng.standard_normal(n)
    # gentle low-pass: cumulative smoothing
    lp = np.convolve(noise, np.ones(8) / 8, mode="same")
    return _limit(lp * env)

def _click(rng) -> np.ndarray:
    n = int(SR * 0.060)                      # ~60 ms
    t = np.arange(n) / SR
    down = np.exp(-t / 0.008) * rng.standard_normal(n)
    up = 0.6 * np.exp(-np.clip(t - 0.012, 0, None) / 0.010) * rng.standard_normal(n)
    band = np.convolve(down + up, np.ones(6) / 6, mode="same")
    return _limit(band)

def generate(out_dir: Path) -> None:
    rng = np.random.default_rng(0)           # fixed seed → byte-identical
    _write(out_dir / "click.wav", _click(rng))
    _write(out_dir / "key.wav", _key(rng))

if __name__ == "__main__":
    generate(Path("guidebot_recorder/sfx"))
    print("wrote guidebot_recorder/sfx/{click,key}.wav")
```

Create `guidebot_recorder/sfx/__init__.py` (empty), then generate the assets:

```bash
uv run scripts/gen_sfx.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/video/test_gen_sfx.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the assets ship in the wheel (packaging is a no-op check)**

Run: `python -m build --wheel 2>/dev/null && python - <<'PY'
import zipfile, glob
whl = sorted(glob.glob("dist/*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
assert any(n.endswith("sfx/click.wav") for n in names), names
print("sfx assets present in wheel")
PY`
Expected: prints "sfx assets present in wheel". If NOT present, add to `pyproject.toml` a hatchling force-include for `guidebot_recorder/sfx/*.wav`; otherwise no pyproject change.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_sfx.py guidebot_recorder/sfx/__init__.py guidebot_recorder/sfx/click.wav guidebot_recorder/sfx/key.wav tests/unit/video/test_gen_sfx.py
git commit -m "feat(sfx): committed key/click WAVs + deterministic gen_sfx.py generator"
```

### Task 4.2: `video/sfx.py` — `build_sfx_bed`

**Files:**
- Create: `guidebot_recorder/video/sfx.py`
- Test: `tests/unit/video/test_sfx.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Produces: `build_sfx_bed(events: list[tuple[str, float]], total: float, out: Path, *, click_path: Path, key_path: Path, gain_db: float) -> None` — builds one bounded (≤3-input) SFX bed, exactly `total` seconds.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/video/test_sfx.py
import pytest
from pathlib import Path
from importlib.resources import files, as_file

pytestmark = pytest.mark.ffmpeg

def _assets():
    return (files("guidebot_recorder.sfx").joinpath("click.wav"),
            files("guidebot_recorder.sfx").joinpath("key.wav"))

def test_build_sfx_bed_length_and_bounded_inputs(tmp_path):
    from guidebot_recorder.video.sfx import build_sfx_bed
    from guidebot_recorder.video.mux import probe_duration
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
    from guidebot_recorder.video.sfx import build_sfx_bed
    from guidebot_recorder.recorder.render import RenderError
    click, key = _assets()
    with as_file(click) as cp, as_file(key) as kp, pytest.raises((ValueError, RenderError)):
        build_sfx_bed([("click", -0.1)], total=2.0, out=tmp_path / "x.wav",
                      click_path=Path(cp), key_path=Path(kp), gain_db=-12.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/video/test_sfx.py -v`
Expected: FAIL (no module).

- [ ] **Step 3: Write minimal implementation**

```python
# guidebot_recorder/video/sfx.py
"""Build one language-independent SFX bed (bounded 3-input ffmpeg graph)."""
from __future__ import annotations
from pathlib import Path
from guidebot_recorder.video.mux import SAMPLE_RATE, _run_to_output, ffmpeg_bin

def build_sfx_bed(events, total, out, *, click_path, key_path, gain_db) -> None:
    out = Path(out)
    for kind, offset in events:
        if offset < 0:
            raise ValueError(f"sfx offset must be >= 0, got {offset}")
    by_kind = {"click": (Path(click_path), []), "key": (Path(key_path), [])}
    for kind, offset in events:
        if kind in by_kind:
            by_kind[kind][1].append(offset)
    sources = [(path, offs) for path, offs in by_kind.values() if offs]
    if not sources:
        return  # no events → no bed
    cmd = [ffmpeg_bin(), "-y", "-f", "lavfi", "-t", f"{total:.6f}",
           "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
    for path, _ in sources:
        cmd += ["-i", str(path)]
    filters, mix_labels = [], ["[0:a]"]
    for idx, (path, offs) in enumerate(sources, start=1):
        base = f"[{idx}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo"
        if len(offs) == 1:
            filters.append(f"{base},adelay={int(round(offs[0]*1000))}:all=1[s{idx}_0]")
            mix_labels.append(f"[s{idx}_0]")
        else:
            splits = "".join(f"[s{idx}_{j}]" for j in range(len(offs)))
            filters.append(f"{base},asplit={len(offs)}{splits}")
            for j, off in enumerate(offs):
                filters.append(f"[s{idx}_{j}]adelay={int(round(off*1000))}:all=1[d{idx}_{j}]")
                mix_labels.append(f"[d{idx}_{j}]")
    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0[m]")
    filters.append(f"[m]volume={gain_db}dB[out]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-t", f"{total:.6f}"]
    _run_to_output(cmd, out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/video/test_sfx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/video/sfx.py tests/unit/video/test_sfx.py
git commit -m "feat(video): build_sfx_bed (bounded 3-input SFX bed, mandatory -t total)"
```

### Task 4.3: Render integration — collect SFX events + mix into each language bed

**Files:**
- Modify: `guidebot_recorder/recorder/render.py` (add `sfx_events`, the `on_sfx` sink, offset conversion with a fail-loud raise, and the per-language mix-in inside `_mux_tracks_for_timeline`/`_assemble_audio_tracks`)
- Test: `tests/unit/recorder/test_render.py` and/or `tests/unit/video/test_sfx.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Consumes: `Recorder(..., on_sfx=sink)` (2.x), `build_sfx_bed` (4.2).
- Produces: when `cfg.sound.enabled` and ≥1 event, a shared `sfx-bed.wav` mixed (with `alimiter=limit=0.95:level=disabled`) into each `bed-<lang>.wav`; both no-popup and popup paths covered (both route through `_assemble_audio_tracks`).

- [ ] **Step 1: Write the failing test** (bed length + peak stays below 0 dBFS)

```python
@pytest.mark.ffmpeg
def test_narration_plus_sfx_bed_matches_duration_and_no_clip(tmp_path):
    # Build a narration bed + a loud tone, mix in a click bed with the alimiter,
    # assert combined duration within tolerance and astats peak < 0 dBFS.
    # (Use build_audio_bed + build_sfx_bed + the same alimiter mix-in helper render uses.)
    ...
```

(Write a concrete ffmpeg-backed assertion using `probe_duration` and an `astats`-parsed peak `< 0.0`. Keep it in `tests/unit/video/`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/video/test_sfx.py -k no_clip -v`
Expected: FAIL (mix-in helper not implemented).

- [ ] **Step 3: Write minimal implementation** (`render.py`)

- In `run_render`, before the step loop: `sfx_events: list[tuple[str, float]] = []` and
  ```python
  def sfx_sink(kind: str) -> None:
      sfx_events.append((kind, time.monotonic()))
  ```
  Pass `on_sfx=sfx_sink` to the per-step Recorder (render.py:606) **only when** `cfg.sound.enabled` (else `None`).
- After the loop, convert to offsets against `anchor`, filtering by gating and **raising** on a negative offset:
  ```python
  sfx_offsets = []
  for kind, t in sfx_events:
      if kind == "click" and not cfg.sound.click:  continue
      if kind == "key" and not cfg.sound.keys:      continue
      off = t - anchor
      if off < 0:
          raise RenderError(f"ujemny offset SFX ({off}) — błąd zegara renderu")
      sfx_offsets.append((kind, off))
  ```
- Thread `cfg.sound`, `sfx_offsets` into `_assemble_audio_tracks` → `_mux_tracks_for_timeline`. Inside, when `cfg.sound.enabled and sfx_offsets`, build `sfx-bed.wav` once via `build_sfx_bed(...)` (assets resolved via `importlib.resources.as_file`), then for each language mix it into `bed-<lang>.wav` with a two-input `amix normalize=0` + `alimiter=limit=0.95:level=disabled`, re-trimmed `-t total`. Narration is never attenuated; the sfx bed already carries `gain_db=cfg.sound.volume`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/video/test_sfx.py -k no_clip -v`
Expected: PASS.

- [ ] **Step 5: Regression — sound-off path is byte-identical**

Run: `pytest tests/unit/recorder -v`
Expected: PASS; with `sound.enabled=False` (or zero events) no extra ffmpeg input is added and output matches today.

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/video/test_sfx.py
git commit -m "feat(render): collect SFX events + mix subtle key/click bed under each narration track"
```

---

## Phase 5 — Slides + intro

Depends on Phase 1 (overlay/chrome `hide`/`show` + hidden flag) and Phase 3. New `slide/` package mirrors `overlay/`/`chrome/`.

### Task 5.1: `slide/slide.js` + `slide/slide.py` — full-frame card overlay

**Files:**
- Create: `guidebot_recorder/slide/__init__.py`, `guidebot_recorder/slide/slide.js`, `guidebot_recorder/slide/slide.py`
- Test: `tests/unit/slide/test_slide.py` (new dir; real Chromium)

**Interfaces:**
- Produces: JS `window.__guidebot_slide` with `show(card)`, `hide()`, `ensure(card)`, a monotone **shown-token** in the closure, a full-viewport `position:fixed inset:0` opaque-dark `<div data-guidebot-slide>` at `z-index MAX_Z_INDEX`, text via `textContent` (never innerHTML), **hit-testable** (NO `pointer-events:none`). Python `Slide` controller: `show(page, card)`, `ensure(page, card)`, `hide(page)`, `install_context(context)`, with `card = {title, subtitle, notes}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/slide/test_slide.py
import pytest
from collections.abc import AsyncIterator
from playwright.async_api import Page, async_playwright

@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True); pg = await b.new_page()
        try: yield pg
        finally: await b.close()

async def test_show_mounts_card_leaving_page_dom_intact(page):
    from guidebot_recorder.slide.slide import Slide as SlideCtl
    await page.set_content('<main id="app">hello</main>')
    ctl = SlideCtl()
    await ctl.install(page)
    await ctl.show(page, {"title": "<b>T</b>", "subtitle": "S", "notes": None})
    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 1
    assert await page.eval_on_selector("#app", "el => el.textContent") == "hello"  # untouched
    # text escaped, not HTML:
    assert await page.eval_on_selector("[data-guidebot-slide]", "el => el.innerHTML").count("<b>") == 0
    # shown-token present:
    assert await page.evaluate("!!window.__guidebot_slide.token()") is True
    await ctl.hide(page)
    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/slide/test_slide.py -v`
Expected: FAIL (no `slide` package).

- [ ] **Step 3: Write minimal implementation**

`slide.js`: an IIFE mirroring `cursor.js` — reads `window.__guidebot_slide_config` (default theme dark), exposes `show(card)`/`hide()`/`ensure(card)`/`token()`. `show` sets a monotone token, mounts one `[data-guidebot-slide]` full-viewport fixed opaque-dark div at `MAX_Z_INDEX`, centered title/subtitle/notes set via `textContent`, subtle fade-in; does **not** set `pointer-events:none`. `hide` removes the node (keeps the token concept per spec; `ensure` re-mounts from `card`). `slide.py`: mirror `Overlay` (`read_text` the JS, prelude config, `install`, `install_context`, `show`/`ensure`/`hide` calling `page.evaluate`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/slide/test_slide.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/slide/ tests/unit/slide/test_slide.py
git commit -m "feat(slide): full-frame card overlay (hit-testable, textContent-escaped, shown-token)"
```

### Task 5.2: Compile — `slide` → null cached action + verbose title

**Files:**
- Modify: `guidebot_recorder/recorder/compile.py` (`_compile_step` early return; `_short`)
- Test: `tests/unit/recorder/test_compile.py` (extend)

**Interfaces:**
- Consumes: `Step.command_kind()=="slide"` (0.6).
- Produces: a slide step compiles to `None` (like say/navigate); the Reasoner is never called; slot count preserved. `_compiled_from` (render.py) and `_instruction` (compile.py) unchanged (gated by `requires_target()`).

- [ ] **Step 1: Write the failing test**

```python
async def test_slide_compiles_to_null_without_reasoner(...):
    # A scenario with a slide step compiles: actions[i] is None, Reasoner not invoked,
    # len(actions) == len(steps). Use the existing compile test harness + a fake Reasoner
    # that raises if called.
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/recorder/test_compile.py -k slide -v`
Expected: FAIL (slide falls through to the target path → Reasoner called / error).

- [ ] **Step 3: Write minimal implementation** (`compile.py`, `_compile_step`, alongside say/navigate/targetless-wait early returns ~L539-548)

```python
    if kind == "slide":
        return None
```

And teach `_short` (compile.py:114-125) to render a slide title:

```python
    if step.slide is not None:
        return step.slide.title or step.slide.subtitle or "slide"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/recorder/test_compile.py -k slide -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/recorder/compile.py tests/unit/recorder/test_compile.py
git commit -m "feat(compile): slide step compiles to null cached action"
```

### Task 5.3: Render — card loop (paint before narration, hide/show, rule 3 + token, intro bootstrap)

**Files:**
- Modify: `guidebot_recorder/recorder/render.py`
- Test: `tests/unit/recorder/test_render.py` (real Chromium / targeted)

**Interfaces:**
- Consumes: `Slide` controller (5.1), `Overlay.hide/show` + `Chrome.hide/show` (Phase 1), `IntroConfig`/`Slide` models (Phase 0).
- Produces: `run_render` tracks `card_active`; a `slide` step paints the card **before** narration and hides cursor/chrome; a following `say` keeps the card via a **card-aware ensure** (`slide.ensure` + re-assert `overlay.hide`/`chrome.hide`); any other kind (incl. `teach`) **dismisses** the card, first **asserting the shown-token** (fail-loud `RenderError` if a navigation destroyed the card mid-`say`); `"slide"` added to the popup-close guard set (render.py:633); auto-intro replaces the white bootstrap when `cfg.intro.enabled`.

- [ ] **Step 1: Write the failing tests** (drive minimal scenarios; assert картинки)

```python
async def test_slide_step_paints_card_and_hides_layers(...):
    # A [slide, say] pair: card present during the say; cursor/chrome display:none.
    ...
async def test_teach_after_slide_dismisses_card(...):
    # [slide, teach-something]: before the teach runs, the card is gone and cursor shown.
    ...
async def test_navigation_wiping_card_mid_say_fails_loud(...):
    # A say-over-card whose page self-navigates (fresh tokenless context) → RenderError,
    # not silent narration over the raw page.
    ...
async def test_intro_replaces_white_bootstrap_when_enabled(...):
    # cfg.intro.enabled=True → first frame is the intro card, not the white document.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/recorder/test_render.py -k "slide or intro or card" -v`
Expected: FAIL (no card logic).

- [ ] **Step 3: Write minimal implementation** (`render.py`)

Follow the slides spec's Render integration precisely:
- Build a `Slide` controller and `slide.install_context(context)` next to overlay/chrome (render.py ~514-517).
- `card_active: bool` in `run_render`. Card-aware visual prep **before** the narration block (render.py:581):
  - `kind == "slide"`: dismiss any prior card, `slide.show(active_page, card)`, `overlay.hide` + `chrome.hide`, `card_active = True`.
  - `kind == "say"` and `card_active`: card-aware ensure = `slide.ensure(active_page, card)` + re-assert `overlay.hide`/`chrome.hide`; discriminate rewrite (token present → repair) vs navigation (fresh tokenless API → raise `RenderError`).
  - any other kind (incl. `teach`) while `card_active`: **assert shown-token present** (else `RenderError`), then `slide.hide` + `overlay.show`/`chrome.show`, `card_active = False`; run the step normally.
- `_render_step` for `kind == "slide"`: no target/ripple; with narration → one card-aware ensure (`slide.ensure` + re-hide) + `page.screenshot()`; with no narration → SPA-safe hold for `step.slide.hold` s (re-assert card on a short cadence, not a blind sleep).
- Add `"slide"` to the popup-close guard set at render.py:633: `kind in {"say", "navigate", "wait", "slide"}`.
- Auto-intro: when `cfg.intro.enabled`, replace the white bootstrap (render.py:555) with the intro card (`config.title` + `intro.subtitle`/`notes`), `overlay.hide` + `chrome.hide`, force a frame, set `anchor`, `card_active = True`; `enabled: false` keeps today's white frame.
- Card is always shown on `_active_page(page, popup)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/recorder/test_render.py -k "slide or intro or card" -v`
Expected: PASS.

- [ ] **Step 5: Regression — full render suite + a real end-to-end render with a slide + intro**

Run: `pytest tests/unit/recorder -v` (+ one ffmpeg/network render of a slide+intro scenario)
Expected: PASS; intro-off renders identical to today.

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): slide cards + auto-intro (card-aware ensure, rule-3 token assert, fail-loud)"
```

---

## Phase 6 — Demo scenario + docs (+ recompile)

Depends on all prior phases. This is the rollout: enable the toggles in the working demo, simplify the popup step (needs Codex recompile), and update docs.

### Task 6.1: Update `s.yaml` demo (render-only toggles + popup step)

**Files:**
- Modify: `s.yaml` (repo-root, untracked working demo — do NOT commit it per the rollout spec)
- Recompile: `s.compiled.yaml` via Codex (`guidebot compile s.yaml`) — the popup-step change is structural.

- [ ] **Step 1:** Add render-only toggles to `s.yaml` `config`: `cursor.width/height` (46/62), `cursor.click {color, scale, flash}`, `typing {animate: true, speed: 60}`, `sound {enabled: true, click: true, keys: true, volume: -12}`, `intro {enabled: true, subtitle, notes}`. `chrome.enabled` is already `true` (s.yaml:7-8).
- [ ] **Step 2:** Replace the popup `teach` step (s.yaml:19) with `enterText: {into: "pole email", text: "koparka@poczta.wp.pl"}` + its `say`.
- [ ] **Step 3:** Recompile with Codex: `guidebot compile s.yaml` (the `teach`→`enterText` command-kind switch requires it; render preflight would otherwise fail loudly).
- [ ] **Step 4:** Smoke render `s.yaml` and eyeball the video (centered bigger cursor, flash, typed email with key ticks + click, browser bar, intro card). **Do NOT commit** `s.yaml`/`s.compiled.yaml`/`test.mp4` — they are the maintainer's untracked working files.

(No commit for this task — the demo files stay untracked.)

### Task 6.2: Docs (bilingual EN + PL)

**Files:**
- Modify: `docs/en/scenario-reference.md`, `docs/pl/scenario-reference.md` — new `config` blocks (`cursor.click`, `typing`, `sound`, `intro`) and the `slide` step.
- Modify: `docs/en/scenario-files.md`, `docs/pl/scenario-files.md` — narrative examples.
- Modify: `README.md` — the new blocks, the `slide` step, and the sound/typing/intro features in both the English and Polish halves.

- [ ] **Step 1:** Document each new block with its fields, defaults, and the render-only/recompile note (cross-reference the recompile matrix: only `slide` STEPS and the popup step need `guidebot compile`).
- [ ] **Step 2:** Document the `slide` step YAML (`slide: {title, subtitle?, notes?, hold?}` + optional sibling `say`), the single-language-picture / narration-switches multilingual rule, and that a narrated slide can't linger after narration (follow it with a silent slide to hold).
- [ ] **Step 3:** Where trivially checkable, run `mkdocs build` to confirm docs parity.
- [ ] **Step 4: Commit**

```bash
git add docs/ README.md
git commit -m "docs: document cursor.click/typing/sound/intro blocks and the slide step (EN+PL)"
```

### Task 6.3: Final integration verification

- [ ] **Step 1:** Full unit suite green (minus the two known pre-existing failures): `pytest -q`.
- [ ] **Step 2:** `guidebot validate s.yaml` accepts the updated demo; a minimal all-toggles-on scenario validates and renders.
- [ ] **Step 3:** Confirm back-compat: an existing example (no new blocks) renders byte-identically except the centered cursor.
- [ ] **Step 4:** Open a PR summarizing the batch and the recompile matrix; request review.

---

## Self-review checklist (run before execution)

- **Spec coverage:** R1 (bigger cursor + flash) → 0.1, 1.1, 1.2; R2 (centered start) → 1.1 (`CFG.start`), 1.2 (`viewport`), 3.1; R3 (typing) → 0.2, 2.2, 3.1; R4/R5 (key/click sound) → 0.3, 2.1, 4.1-4.3; R6 (popup step) → 6.1; R7 (chrome bar) → 6.1 (already `enabled`); R8 (slide + intro) → 0.6, 1.3, 5.1-5.3. All covered.
- **Ordering:** overlay bundle (Phase 1) lands before render card logic (Phase 5); `ripple(flash=)` (1.2) lands before recorder calls it (2.1); config foundation (Phase 0) first. Acyclic.
- **Fail-loud:** SFX negative offset raises `RenderError` (4.3); rule-3 token assert raises (5.3); no bare `assert`/`except: pass`.
- **Recompile:** only 6.1 (popup step) and any added slide STEP need Codex compile; everything else render-only (guarded by 0.5).
