# Character-by-character typing — design

Date: 2026-07-15
Status: REVIEWED — GO. Two genuine Fable-model review rounds ran on 2026-07-15 (each
round: fresh read-only agents, code-grounded); every finding was judged and applied.
(The pre-handoff "review #1/#2" provenance had been confabulated by fork agents and
was reset before these real reviews ran; see
docs/superpowers/2026-07-15-video-polish-HANDOFF.md.) Code anchors re-verified against
the current tree on 2026-07-15.
Extends the main compile/render design. Render-only input animation, plus the
shared `on_sfx` callback contract consumed by the sound-effects spec.

## Goal and user-visible acceptance

Today `Recorder.enter_text` (`recorder/recorder.py:67-69`) calls
`locator.fill(text)`, which pastes the whole value instantly — viewers cannot
follow what is typed. When enabled, **render** types **character by character**.
Compile always keeps the instant path (no video, must stay fast).

- A rendered `enterText`/literal-`teach` type action types one character at a time
  at a configurable cadence.
- Each character emits a `key` sound event; each real click emits a `click`
  event. Sound synthesis/mixing is out of scope here (see
  `2026-07-15-sound-effects-design.md`); this spec only defines and emits the
  events.

Render-only: **no recompile**.

## Approved direction

### 1. Config

New block in `models/config.py`:

```python
class TypingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    animate: bool = False    # opt-in (keeps existing renders inert); True → char-by-char
    speed: int = Field(default=60, gt=0)  # ms PER CHARACTER (a delay; higher = SLOWER)


# NOTE: `typing.speed` is a per-character *delay in ms* (higher = slower). Do not
# confuse it with `CursorConfig.speed` (config.py:59), which is a *rate in px/ms*
# (higher = faster). Same field name, inverted semantics — kept as `speed` to match
# the approved config surface; the loud comment above is the disambiguation.
```

Added to `Config` as `typing: TypingConfig = Field(default_factory=TypingConfig)`.
Not part of `config_hash()`. **Default `animate=False`** so unmodified scenarios
render exactly as today (see the rollout back-compat contract); the demo `s.yaml`
sets `animate: true`.

### 2. Recorder contract

```python
class Recorder:
    def __init__(
        self,
        page: Page,
        overlay: Overlay | None,
        settle_ms: float = 280,
        *,
        type_delay_ms: float | None = None,
        on_sfx: Callable[[str], None] | None = None,
    ) -> None:
        ...
```

- `type_delay_ms is None` → `enter_text` keeps `locator.fill(text)` (instant).
- `type_delay_ms` set → `enter_text` clears the field (`fill("")`), types the value
  one character at a time waiting `type_delay_ms` between characters, calls
  `on_sfx("key")` after each character, then applies the value-correction below.
- The shared ripple lives in `_point_and_prepare`, which serves click/hover/type.
  Thread the intent so only real clicks flash **and** sound:

```python
async def _point_and_prepare(self, target: Target, *, click_sound: bool = False) -> Locator:
    ...
    rippled = False
    if self.overlay is not None:
        box = await locator.bounding_box()
        if box is not None:
            ...
            await self.overlay.move_to(self.page, cx, cy)
            await self.overlay.ripple(self.page, flash=click_sound)
            # Emit the click sound AT the ripple moment — the beat the viewer
            # perceives as the click — BEFORE the settle pause (review #1 fix:
            # emitting after wait_for_timeout(settle_ms) desyncs it by ~280 ms).
            if click_sound and self._on_sfx is not None:
                self._on_sfx("click")
            rippled = True
            await self.page.wait_for_timeout(self.settle_ms)
    # Fallback so a click still sounds exactly once when the overlay is absent or
    # the bounding box could not be measured.
    if click_sound and not rippled and self._on_sfx is not None:
        self._on_sfx("click")
    return locator

async def click(self, target, *, before_click=None):
    locator = await self._point_and_prepare(target, click_sound=True)
    ...

async def hover(self, target):
    locator = await self._point_and_prepare(target)   # no click sound, no flash

async def enter_text(self, target, text):
    locator = await self._point_and_prepare(target)   # no click sound, no flash
    # Control characters (\n, \r, \t) would press Enter/Tab and could submit or
    # blur mid-typing (poisoning the next step's reuse_is_valid). v1 animates only
    # printable literals; the instant path handles anything else.
    if self._type_delay_ms is None or any(c in text for c in "\n\r\t"):
        await locator.fill(text)
        return
    await locator.fill("")
    for i, ch in enumerate(text):
        await locator.press_sequentially(ch)
        if self._on_sfx is not None:
            self._on_sfx("key")
        if i < len(text) - 1:                 # no dead beat after the final char
            await self.page.wait_for_timeout(self._type_delay_ms)
    # Value-correction: honour the frozen literal even if a field mask/formatter
    # rewrote it during per-key events. input_value() only works on
    # <input>/<textarea>/<select>; on contenteditable (where fill() also works) it
    # RAISES, so guard it and fall back to fill() loudly rather than crash render.
    try:
        needs_fix = await locator.input_value() != text
    except PlaywrightError:
        needs_fix = True                      # non-input target → re-issue fill()
    if needs_fix:
        await locator.fill(text)
```

The click event fires at the **ripple** moment (the beat the viewer perceives as the
click) — emitted immediately after `overlay.ripple(...)` and **before** the
`settle_ms` pause (default 280 ms, config.py:62), so the tick lands on the visible
flash instead of ~280 ms late. A single fallback emission covers the no-overlay /
unmeasurable-box case, so a click sounds **exactly once** in every path.
(For the `enter_text` guard below, add `from playwright.async_api import Error as
PlaywrightError` to `recorder.py` — the module exports `Error`, not `PlaywrightError`;
this matches the existing alias in `render.py:27-29`.)

### 3. Render/compile behavioural divergence (explicit)

Compile freezes actions against `locator.fill()` (value set + one `input` event);
animated render replays real per-character `keydown/keypress/input/keyup`. Pages
with keystroke handlers — autocomplete dropdowns, input masks, live validation
(e.g. the demo types into the onet.pl login popup) — may mutate the DOM in ways
compile never observed and either fail a later step's `reuse_is_valid` identity
check (render.py:777) or reshape the value.

Mitigations, all in this spec:
- the trailing value-correction (`input_value() != text → fill(text)`) guarantees
  the frozen *value* regardless of masks (note it does **not** clean up DOM residue
  such as an open autocomplete dropdown, which could still occlude/ambiguate the next
  step — acceptable for a default-off v1);
- **`typing.animate: false` is the per-scenario escape hatch** for fields that
  misbehave under real keystrokes;
- documentation must state that masked/formatted/autocomplete fields should keep
  `animate: false`.

**Deferred to a later phase (review #1 note):** a *per-step* `animate` override on
`enterText` (mirroring the existing `step.navigate_type_override()` →
`chrome.type_on_navigate` pattern, render.py:755-758) would let one misbehaving field
opt out without disabling animation for the whole video. Deliberately **out of v1
scope** to keep the surface small: the feature is opt-in, and a scenario that hits a
bad field can flip the global `typing.animate: false`. Adding `EnterText.animate:
bool | None = None` later is render-only (it is not part of `_compiled_from`, which
for `enterText` is only `step.enter_text.into` — render.py:393-394), so it needs no
recompile and can be added without disturbing frozen actions.

`press_sequentially` handles printable text; v1 targets printable frozen literals
(emails, names). Control characters (`\n`, `\t`) are out of scope — `\n` would press
Enter and could submit a form mid-typing. If a frozen `input_text` contains a
control character, the typed path must fall back to `fill(text)` for that whole
value (the plan adds this guard).

### 4. Wiring

- `recorder/render.py` (~line 606, the per-step render Recorder) is built with
  `type_delay_ms = cfg.typing.speed if cfg.typing.animate else None` and an `on_sfx`
  sink (defined in the sound-effects spec; `None` when sound is off).
- `recorder/compile.py` constructs `Recorder(active_page, overlay=None)` at
  **lines 306 and 408** (line 628 is a `recorder.enter_text(...)` *call*, not a
  constructor) — unchanged: no `type_delay_ms`, no `on_sfx` → instant `fill`, fast
  compile.
- `recorder/render.py:272` (`_prepare_main_after_popup_close`) constructs a third
  `Recorder(page, None, ...)` used only for readiness — it must **not** receive
  `type_delay_ms`/`on_sfx`.
- **Cross-spec ordering (review #1):** the `_point_and_prepare` rewrite above calls
  `self.overlay.ripple(self.page, flash=click_sound)`, but `Overlay.ripple` gains its
  keyword-only `flash` param only in the cursor spec
  (`2026-07-15-cursor-visibility-design.md`). The cursor spec's `Overlay.ripple(page,
  *, flash=False)` change must land **before or together with** this rewrite, else
  render raises `TypeError: unexpected keyword argument 'flash'`. See the rollout
  spec's ordering section (this spec owns the `click_sound` intent; the cursor spec
  owns the `flash=` pass-through and the ripple appearance).

## Shared contract (defined here, consumed by sound-effects)

`on_sfx: Callable[[str], None]`. Kinds are exactly `"click"` and `"key"`. The
callback receives only the kind; the render loop timestamps it with
`time.monotonic()` and converts to a timeline offset. `"click"` is emitted whenever
`click_sound and self._on_sfx is not None` (independent of ripple guards); `"key"`
is emitted once per typed character on the animated path only. Timestamping, offset
conversion, gating, and audio synthesis/mixing are in
`2026-07-15-sound-effects-design.md`.

## Files touched

- `guidebot_recorder/models/config.py` — `TypingConfig`, `Config.typing`.
- `guidebot_recorder/recorder/recorder.py` — new params, `click_sound` on
  `_point_and_prepare`, animated `enter_text` with value-correction.
- `guidebot_recorder/recorder/render.py` — Recorder construction with typing +
  `on_sfx`.
- `guidebot_recorder/recorder/compile.py` — unchanged behaviour (documented).
- Tests under `tests/unit/recorder/`.

## Testing (TDD)

Tests drive real Chromium pages (matching `tests/unit/recorder/test_recorder.py`):

- `enter_text` with `type_delay_ms=60` into a real input types char-by-char (assert
  via a page-side `keydown` counter through `page.evaluate`, and that
  `input_value()` builds up) and emits exactly `len(text)` `on_sfx("key")` calls;
  the value equals `text` afterwards.
- `enter_text` with `type_delay_ms=None` calls `fill` once and emits zero key
  events (existing `test_enter_text_fills` still passes).
- Value-correction: a field with a JS formatter still ends at `text`.
- Contenteditable target with `type_delay_ms=60`: types char-by-char and ends at
  `text` **without raising** (the `input_value()` guard falls back to `fill`).
- A frozen literal containing `\n`/`\t` takes the instant `fill` path (no
  per-character events, no mid-typing submit).
- `click()` emits exactly one `on_sfx("click")` even when `overlay=None`, and (with
  an overlay) the emission happens **before** the `settle_ms` wait, not after;
  `hover()`/`enter_text()` emit no click events and draw no flash.
- Render builds the Recorder with animation when `typing.animate`; compile builds
  the instant path.

## Recompile impact

None (render-only). Typing animation and key/click event emission are render-time
only; `config_hash()` and frozen actions are unaffected.
