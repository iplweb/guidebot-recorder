# Cursor visibility — design

Date: 2026-07-15
Status: REVIEWED — GO. Two genuine Fable-model review rounds ran on 2026-07-15 (each
round: fresh read-only agents, code-grounded); every finding was judged and applied.
(The pre-handoff "review #1/#2" provenance had been confabulated by fork agents and
was reset before these real reviews ran; see
docs/superpowers/2026-07-15-video-polish-HANDOFF.md.) Code anchors re-verified against
the current tree on 2026-07-15.
Extends the main compile/render design. Purely render-time cosmetics for the
synthetic pointer.

## Goal and user-visible acceptance

Make the synthetic cursor easier to follow in a training film:

1. **Start centered.** The cursor's first painted position is the centre of the
   viewport, not the top-left corner `(0, 0)`. This is the **one intended universal
   change** — every render is affected, with no opt-out (see the rollout spec's
   back-compat section).
2. **Bigger pointer.** The scenario can enlarge the arrow through the existing
   `cursor.width`/`cursor.height` fields; code defaults stay at `34×46`.
3. **Stronger click flash.** An opt-in, configurable, larger/brighter click ripple
   with an optional filled flash. **Defaults reproduce today's ripple exactly**, so
   the feature is inert until the scenario opts in.

All three are **render-only**. None participates in `config_hash()`; no recompile.

## Approved direction

### 1. Centered start

`Overlay.__init__` gains an optional viewport:

```python
class Overlay:
    def __init__(self, cursor: CursorConfig | None = None, viewport: Viewport | None = None) -> None:
        self.cursor = cursor or CursorConfig()
        if viewport is not None:
            self.pos = (viewport.width / 2, viewport.height / 2)
        else:
            self.pos = (0.0, 0.0)
        ...
```

- `recorder/render.py:513` changes `Overlay(cfg.cursor)` → `Overlay(cfg.cursor, cfg.viewport)`.
- The bootstrap frame (`render.py:555-559`) already calls `_ensure_visuals`, which
  restores `overlay.pos` at `ms=0`, so the first captured frame shows the cursor
  centred.
- **Seed the JS mount position too (robust first frame — review #1 finding).**
  Python-side `overlay.pos` alone is not enough: `cursor.js` initialises every fresh
  document's `state` to `(0, 0)` (cursor.js:37-42) and mounts the cursor immediately
  from the context init script, *before* the first `_ensure_visuals` restore. With a
  centred `pos`, the screencast could otherwise encode one top-left corner frame that
  then jumps to centre (newly visible only because the JS default no longer matches
  `pos`). Fix: seed the start through the existing appearance prelude — add
  `"start": [viewport.width / 2, viewport.height / 2]` to
  `window.__guidebot_cursor_config` in `Overlay.__init__` (only when a viewport is
  given), and have `cursor.js:40-41` read `CFG.start` instead of the hard-coded `0`
  for `state.x`/`state.y`. This centres the very first JS mount in every document with
  zero extra round-trips. **Trade-off (accepted):** because the init script is static
  and context-wide, *every* new document (not just the first) mounts the cursor at
  centre, so after each `navigate` the cursor sits centred during page load until
  `_ensure_visuals` snaps it to `overlay.pos` (render.py:769). This merely *relocates*
  a pre-existing cosmetic glitch that today parks the fresh-document cursor at (0,0);
  centre is the neutral parked position and there is no mechanism to avoid it while the
  init script is static — acceptable. (Popups are unaffected: `_prime_visuals` restores
  `overlay.pos` = last cursor position, the desired continuity; popup startup is
  additionally masked by `visual_ready_delay`, render.py:717.)
- **Back-compat:** `viewport` is optional and defaults to `None`; `Overlay()` /
  `Overlay(cursor)` keep the `(0, 0)` origin (and omit `CFG.start`, so cursor.js
  falls back to `0`), so existing overlay unit tests are unaffected. No config knob —
  centre is the render default.

### 2. Bigger pointer

Documentation only. Authors set `cursor.width`/`cursor.height` (e.g. `46`/`62`).
Code defaults remain `34`/`46`.

### 3. Stronger click flash

New pydantic sub-model in `models/config.py`. **Defaults equal today's hard-coded
ripple** (cursor.js:227 `rgba(37,99,235,.9)`, cursor.js:234 `scale(3.25)`, no
flash), so an unmodified scenario renders an identical ripple:

```python
class CursorClick(BaseModel):
    model_config = ConfigDict(extra="forbid")   # no aliased fields → no populate_by_name
    color: str = "rgba(37,99,235,.9)"           # today's ring colour
    scale: float = Field(default=3.25, gt=0)    # today's expansion multiple; > 0
    flash: bool = False                         # opt-in filled disc under the ring
```

Added to `CursorConfig`:

```python
class CursorConfig(BaseModel):
    ...
    click: CursorClick = Field(default_factory=CursorClick)
```

`overlay/overlay.py` extends the appearance prelude
(`window.__guidebot_cursor_config`) with a `click` key
(`{"color", "scale", "flash"}`).

`overlay/cursor.js` `ripple()` (currently ~lines 211-240) reads `CFG.click` for
ring colour and end-scale (fallbacks = today's blue / `3.25`). **The filled flash
is drawn only when the caller requests it AND `CFG.click.flash` is true.** Draw the
flash as a second short-lived filled disc beneath the ring (`opacity .55 → 0`,
`scale .2 → ~2`), reusing `styleTransient` + `removeAfterAnimation`; no persistent
DOM.

### Flash only on real clicks (not hover/type)

The ripple is emitted from `Recorder._point_and_prepare` (recorder.py:44), shared
by **click, hover and type**. To avoid a "click" flash on hovers and before typing,
thread the click intent — the same `click_sound: bool` bit the typing spec adds to
`_point_and_prepare` — into the ripple call:

```python
await self.overlay.ripple(self.page, flash=click_sound)
```

`Overlay.ripple(page, *, flash: bool = False)` forwards `flash` to the JS
`ripple(flash)`. Hover/type get the plain ring (today's look); only real clicks add
the filled flash. Ring colour/scale from `CFG.click` still apply to every ripple,
but with inert defaults that is a no-op until opted in.

**Plumbing note (review #1):** `overlay.py:109` currently evaluates the literal
`"() => window.__guidebot_cursor.ripple()"`; it must become an argument-passing
`page.evaluate("(f) => window.__guidebot_cursor.ripple(f)", flash)`, and JS
`ripple()` (cursor.js:211) gains a `flash` parameter. `API_VERSION` stays `1` — the
same-version reuse guard (cursor.js:24-34) is fine because production injects exactly
one script version per run; no version bump is needed.

## Scope boundary

This spec covers ripple *appearance*. Binding a click **sound** to the same click
event is in `2026-07-15-sound-effects-design.md`, via the `on_sfx` contract defined
in `2026-07-15-typing-animation-design.md` (both the sound and the flash gate on
the identical `click_sound` intent).

## Files touched

- `guidebot_recorder/models/config.py` — `CursorClick`, `CursorConfig.click`.
- `guidebot_recorder/overlay/overlay.py` — `viewport` arg, centred `pos`, prelude
  `click` + `start` keys, `ripple(..., flash=...)` (argument-passing evaluate).
- `guidebot_recorder/overlay/cursor.js` — configurable ring + optional flash;
  `state.x/y` seeded from `CFG.start` (else `0`); `ripple(flash)` param.
- `guidebot_recorder/recorder/render.py` — `Overlay(cfg.cursor, cfg.viewport)`.
- `guidebot_recorder/recorder/recorder.py` — the ripple call at `_point_and_prepare`
  (recorder.py:44) becomes `await self.overlay.ripple(self.page, flash=click_sound)`.
  **Ordering dependency:** `click_sound` is added to `_point_and_prepare` by the
  typing spec (`2026-07-15-typing-animation-design.md` §2); the `Overlay.ripple`
  `flash=` param (this spec) must land **before or with** that change (else render
  raises `TypeError: unexpected keyword argument 'flash'`). If this spec is
  implemented first/standalone, it introduces `click_sound` itself with the identical
  signature **and has `click()` pass `click_sound=True`** (else the flash is
  unreachable). See the rollout spec's ordering section.
- Tests under `tests/unit/overlay/`.

## Testing (TDD)

- `Overlay(cursor, viewport)` → `pos == (w/2, h/2)`; `Overlay()` / `Overlay(cursor)`
  → `pos == (0.0, 0.0)` (existing tests keep passing).
- Appearance prelude JSON contains a `click` object; `CursorConfig()` yields a
  `CursorClick` equal to today's values; `CursorClick` rejects unknown keys.
- Ripple honours colour/scale and draws the flash only when `flash=True` is passed
  **and** configured. Capture synchronously inside one `page.evaluate` (call
  `ripple(true)` from JS and read the created element's computed style immediately),
  because the element is removed ≤600 ms later (`removeAfterAnimation`,
  cursor.js:205-209) — no sleep-based assertions.
- `Overlay.ripple(page, flash=False)` draws no flash (hover/type path).

## Recompile impact

None (render-only). `config_hash()` is unchanged; `config.cursor` (the new `click`
block and the centred start) never invalidates a compiled sidecar.
