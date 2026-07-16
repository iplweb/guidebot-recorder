# Text slides + auto-intro title card — design

Date: 2026-07-15
Status: REVIEWED — GO. Two genuine Fable-model review rounds ran on 2026-07-15 (each
round: fresh read-only agents, code-grounded); every finding was judged and applied
(incl. the review #2 rule-3 token-assert). (The pre-handoff "review #1/#2 = GO"
provenance had been confabulated by fork agents and was reset before these real
reviews ran; see docs/superpowers/2026-07-15-video-polish-HANDOFF.md.) Code anchors
re-verified against the current tree on 2026-07-15.
Adds full-frame text "slides" (title/subtitle/notes) usable both as an opening
title card (auto-intro, replacing today's blank white bootstrap) and as a `slide`
step anywhere in the flow. On-screen text is not spoken; narration stays separate
via `say`.

## Design decisions (self-review during drafting — NOT a Fable review)

- **DOM overlay, not set_content** — slides are rendered as a **DOM overlay injected into the current
  document** (like the cursor/chrome layers), NOT via `page.set_content`.
  `set_content` would wipe the app document built by prior navigate/click steps, so
  the next compiled target step would fail `reuse_is_valid` (`render.py:777`) and
  compile/render would permanently diverge (compile keeps `slide` a no-op). The
  overlay approach leaves the document intact and also works when the card is shown
  over the popup page.
- **persistent hidden flag** — cursor/chrome are hidden through a **persistent `hidden` flag on
  their JS API**, honored by `ensure()`, not a one-shot `display:none`. Because
  `_ensure_visuals` force-restores both layers at the top of every step
  (`render.py:579, 602, 747`), a one-shot hide would be undone by the first `say`
  step over the card. (Confirmed against `tests/unit/overlay/test_overlay.py`:
  `set_content` does NOT re-mount the overlays, so the old "hide after set_content"
  reasoning was wrong on both counts.)
- **paint before narration** — the card is painted in the **run_render step loop, before**
  narration is placed/awaited (`render.py:581–593`), because `_render_step` runs
  *after* the narration wait and never receives the segments. `_render_step` only
  handles the silent `hold`.

## Goal and user-visible acceptance

- Instead of a blank white window at the start, an optional **title card** shows
  `config.title` + subtitle + notes while the intro narration plays.
- A new **`slide`** step renders a full-frame text screen anywhere in a scenario
  **without disturbing the underlying page**.
- Slide text is **shown, not spoken**; narration is provided separately by `say`.
- Auto-intro has an **on/off switch**; off keeps today's white bootstrap.

## The `slide` step (`models/scenario.py`)

```yaml
steps:
  - slide:
      title: "Logowanie do systemu"
      subtitle: "Krok po kroku"       # optional
      notes: "Materiał szkoleniowy"    # optional
      hold: 2.5                        # optional; seconds to hold when no `say`
    say: "Zaczynamy."                  # optional narration (separate, sibling key)
```

- `Slide` model: `{title: str | None = None, subtitle: str | None = None,
  notes: str | None = None, hold: float = 2.5}`, `extra="forbid"`. A model validator
  requires **at least one** of `title`/`subtitle`/`notes` to be non-empty.
- **`hold` lives inside the `Slide` object**, not on `Step`: with
  `Step`'s `extra="forbid"`, a top-level `hold` would otherwise be silently legal on
  `click`/`enterText`/etc. `say` remains a top-level sibling of `slide` exactly like
  it is for other steps.
- Pacing: when the step has a `say`, it is paced by narration and **`hold` is
  ignored**; when it has no `say`, the card holds `hold` seconds. **Consequence
  (documented, deliberate — review #2):** because a narrated slide's `hold` is ignored
  and any following non-`say`/non-`slide` step dismisses the card (rule 3, incl. a
  targetless `wait` which is a pure sleep, render.py:771-773), there is **no way to
  linger on a narrated slide after its narration ends** — to hold a card *after*
  speech, follow it with a silent `slide` (same text, a `hold`, no `say`). Fail-safe,
  just non-obvious.
- Add `slide` to `PRIMARY_COMMANDS` and to the "exactly one primary command"
  exclusivity validator alongside `teach`/`navigate`/`click`/`hover`/`enterText`/`wait`.
- `command_kind()` returns `"slide"`; `requires_target()` is `False`;
  `narration()` returns the step's `say` text if present (so TTS pre-synthesis and
  the per-language beds treat it exactly like a `say` step, including the
  `translations` requirement: a slide with `say` needs `translations` for every
  alternate audio track; a silent slide must not carry `translations` — this already
  falls out of `Scenario._complete_audio_translations` because `narration()` drives
  it).

## Auto-intro config (`models/config.py`, render-only, NOT in `config_hash`)

```python
class IntroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # no aliased fields → no populate_by_name
    enabled: bool = False
    subtitle: str | None = None
    notes: str | None = None

class Config(BaseModel):
    ...
    intro: IntroConfig = Field(default_factory=IntroConfig)
```

Title comes from `config.title`. `enabled: false` keeps today's white frame.

## Slide renderer (`guidebot_recorder/slide/`)

A JS-controller layer mirroring `overlay/` and `chrome/`:

- `slide/slide.js` exposes `window.__guidebot_slide` with `show(card)`, `hide()`,
  `ensure(card)` where `card = {title, subtitle, notes}`. `show` mounts one
  `<div data-guidebot-slide>` — full-viewport, `position:fixed`, `inset:0`, opaque
  dark background, `z-index` `MAX_Z_INDEX` (2147483647), centered large title,
  medium subtitle, smaller multi-line notes, a subtle fade-in — into
  `document.documentElement`. `hide()`/absence removes it. Idempotent `ensure`, same
  pattern as `cursor.js`.
  - **Do NOT set `pointer-events:none` on the card (review #1 fix).** Unlike
    cursor/chrome/transient layers (which set it — cursor.js:200, chrome.js:104), the
    card must stay **hit-testable**. Rule 3 dismisses the card before any target
    action, so a card should never be up during one; keeping it hit-testable makes any
    accidental card-up **click/hover** (`locator.click()`/`hover()`, no `force` —
    recorder.py:52-65) **fail loud** via Playwright's hit-target check, instead of
    silently clicking *through* the opaque card. **Scope caveat (review #2):** the
    hit-target check only applies to pointer actions. `enterText` uses `locator.fill()`
    (recorder.py:67-69, actionability = visible/enabled/editable, no hit-target check)
    and `waitFor` touches no pointer (recorder.py:76-78), so a card-up `enterText`/
    `waitFor` would *not* be caught by hit-testability — those are protected by **rule
    3 alone** (which now token-asserts and dismisses the card before the step). Keep
    the `pointer-events` decision (it strictly helps click/hover); add a code comment
    explaining the deliberate divergence.
  - **Shown-token:** `show()` stamps a monotone token into the closure/API object;
    `ensure()`/the card-aware ensure use its presence to tell a document rewrite
    (token survives → repair) from a real navigation (fresh context, no token →
    fail-loud). See Render integration rule 2.
  - **Z-index note (review #1):** the card's `MAX_Z_INDEX` equals the cursor's
    (cursor.js:8/123), so card-vs-cursor stacking would be document-order-dependent
    (`ensure()` re-appends a wiped cursor after the card). This is moot **only because
    the persistent `hidden` flag keeps the cursor `display:none` while a card is up** —
    i.e. the flag is load-bearing for stacking too, not just for suppression. (No need
    to raise the card above `MAX_Z_INDEX`; the flag is the guarantee.)
- `slide/slide.py` (`Slide` controller, matching `Overlay`/`Chrome`): reads the
  card fields, builds the appearance prelude, `add_init_script` on the context, and
  `show(page, card)` / `ensure(page, card)` / `hide(page)` helpers (`ensure` is the
  idempotent re-mount used by the card-aware loop path). The card is escaped as
  **text** (`textContent`, never `innerHTML`) so scenario strings cannot inject markup.
- Default theme is hardcoded (dark). Themeable later — YAGNI on per-slide theming.

Because the card is an opaque overlay at max z-index, the underlying document is
untouched; the next target step operates on the real DOM exactly as before.

## Hiding cursor + chrome while a card is up (persistent flag)

The cursor and chrome are also at high z-index and are force-restored every step,
so they must be actively suppressed while a card is shown:

- `overlay/cursor.js` and `chrome/chrome.js` each gain a persistent `hidden` state
  on their API object. `hide()` sets it (and `display:none`); `show()` clears it
  (and restores). Crucially, `ensure()`/`styleCursor`/`chrome.ensure` must **honor
  the flag** — while hidden, `ensure()` keeps `display:none` instead of forcing
  `display:block` (`cursor.js:113`). The flag lives on the API object so it survives
  repeated `_ensure_visuals` calls within one document. A real navigation re-runs the
  init script and resets the flag to visible, which is correct (a `navigate` step
  dismisses the card anyway — see loop rules).
- `Overlay.hide(page)`/`show(page)` and `Chrome.hide(page)`/`show(page)` wrap the JS.

## Render integration (`recorder/render.py`)

A Python-side `card_active: bool` in `run_render` tracks whether a card (intro or a
`slide` step) is currently shown. The step loop gains card-aware visual prep
**before** the narration block:

1. `kind == "slide"`: dismiss any prior card, then `slide.show(active_page, card)`,
   `overlay.hide` + `chrome.hide`, set `card_active = True`. Done **before** placing
   narration so the picture is the card while the `say` plays.
2. `kind == "say"` and `card_active`: keep the card up. In addition to the
   flag-honoring `_ensure_visuals` (which — by the persistent `hidden` flag — does
   **not** resurrect cursor/chrome; that is the whole point, and `_render_step` still
   runs it unconditionally at render.py:747), call a **card-aware ensure** that
   re-mounts the *card* — `slide.ensure(active_page, card)` plus re-assert
   `overlay.hide` + `chrome.hide`. This repairs the card if a live page rewrote its
   document under the narration — the same SPA-mutation hazard the code already guards
   at `render.py:745-747` (the demo targets ad-heavy onet.pl).

   **SPA-repair vs navigation discriminator (review #1 fix).** A benign SPA
   document-rewrite and a spontaneous JS navigation present the *same* observable — a
   missing `[data-guidebot-slide]` node — but must be handled oppositely (repair vs
   fail-loud), and `page.url` is unreliable under `pushState`. Discriminate by **JS
   context identity**: `slide.show()` records a "shown-token" in the
   `window.__guidebot_slide` closure. A document rewrite keeps the JS context and its
   window expandos (`window.__guidebot_slide` survives `document.open`/`set_content`);
   `tests/unit/overlay/test_overlay.py:107-123` shows the *related* fact that after a
   `set_content` the overlay node count is 0 and is *not* auto-remounted (the init
   script did not re-run) — the token-survival itself is proven **directly** by this
   spec's own planned discriminator test (shown-token present after `set_content`). So
   on a rewrite the token survives → **repair** (re-mount the card). A
   real navigation re-runs the context init scripts, yielding a fresh
   `window.__guidebot_slide` with no token → **fail-loud**: raise `RenderError` rather
   than silently narrate over the wrong picture. Concretely: card-aware ensure raises
   iff the API object exists but reports no shown-token (fresh context ⇒ navigation);
   repairs iff the token is present but the card node is gone (rewrite). If
   `window.__guidebot_slide` is **missing entirely** (hostile expando wipe, or an
   evaluate racing a context teardown), re-inject the slide script (the mirrored
   `ensure` pattern, cf. `Overlay.ensure` overlay.py:77-81) — which necessarily yields
   a fresh, tokenless API ⇒ **raise** (the correct fail-loud outcome, made explicit
   rather than left to implementation accident).
3. any page-affecting kind — defined as **any `kind` other than `slide` and `say`**,
   i.e. `teach`/`navigate`/`click`/`hover`/`enterText`/`wait` — while `card_active`:
   **first assert the shown-token is present** (same discriminator as rule 2) — a
   fresh, tokenless `window.__guidebot_slide` at dismissal time means an undetected
   cross-document navigation already destroyed the card *during the preceding `say`*,
   so raise `RenderError` rather than proceed (review #2 fix: without this, a card
   wiped mid-`say` whose next step happens to be page-affecting would silently narrate
   the tail over the raw page — detection must not depend on the *kind* of the
   following step). Then dismiss the card — `slide.hide(active_page)`, `overlay.show`
   + `chrome.show`, `card_active = False` — and run the step normally. **`teach` MUST be in this set
   (review #1 fix):** `teach` is a primary command (scenario.py:15) that resolves to
   click/hover/type and drives a real page action (render.py:775-851); a `teach` after
   a slide/intro would otherwise leave the card up (cursor/chrome hidden) while the
   action runs against the opaque card — the click would either time out on Playwright
   actionability or, worse, execute unseen while its `teach` narration plays over the
   card (`narration()` returns the teach text, scenario.py:95-96), making the failure
   look intentional. Defining the set by exclusion (`not in {"slide", "say"}`) is
   safer than an allow-list a future kind could slip past.

`_render_step` for `kind == "slide"`: no target, no ripple. With narration, the loop
already awaited it → one **card-aware ensure** (`slide.ensure(active_page, card)` **plus
re-assert `overlay.hide` + `chrome.hide`** — the same helper rule 2 uses, NOT a bare
`slide.ensure`; review #2 fix) and force a captured frame (`page.screenshot()`).
Without the re-hide, a page that deleted `window.__guidebot_cursor` during the
narration would have `_ensure_visuals` (render.py:602/747) re-inject a fresh
cursor script whose flag defaults to visible and append a cursor node *above* the card
at equal z-index — painted on top of the card in exactly the frame this path captures.
With no narration, hold the card for `step.slide.hold` seconds as an **SPA-safe wait**
— re-asserting the card via the same card-aware ensure on a short cadence (like the
existing readiness polls) rather than a blind `asyncio.sleep`, so a mid-hold document
rewrite is repaired or fails loud as in rule 2.

Add `"slide"` to the popup-close guard set at `render.py:633`
(`kind in {"say", "navigate", "wait", "slide"}`): a `slide` cannot close a popup, so
without this a spontaneous popup close during a slide would be misclassified as
action-driven and silently swallowed — against the fail-loud rule.

The card is always shown on `_active_page(page, popup)` — the popup when one is open,
else the main page.

### Auto-intro

When `cfg.intro.enabled`, the bootstrap (`render.py:555`,
`set_content("<style>…white…")`) is replaced by: paint the intro card
(`config.title` + `intro.subtitle` + `intro.notes`) as the DOM overlay, `overlay.hide`
+ `chrome.hide`, force a frame, then set `anchor` — with `card_active = True` so the
leading `say` steps narrate over it and the first page-affecting step dismisses it.
`enabled: false` keeps the white frame and `card_active = False`.

**Warm-up caveat:** the recording starts at page creation and `_prime_visuals`
screenshots the pristine page before the bootstrap, so the film opens with a bounded
warm-up frame before the intro card. Whether that frame shows the cursor **centered**
or in the top-left corner depends on the cursor spec landing in the same batch:
`Overlay.pos` initialises to `(0, 0)` today (overlay.py:39), so *without* the cursor
spec's centered-start (`Overlay(cursor, viewport)` + the `CFG.start` prelude seed)
the warm-up shows the corner. Since this batch ships both, the frame shows the
centered cursor on white — but this claim is **contingent on the cursor spec**, not
on anything in this spec. Same pre-roll that exists today; the intro card takes over
from `anchor` onward. Acceptable; documented, not fixed here.

## Compile (`recorder/compile.py`)

`slide` has no target → emit a `null` `cachedAction` (same path as `say`/`navigate`;
the Reasoner is never called for it). **The only real compile change is a
`kind == "slide"` early-return branch in `_compile_step` (compile.py, alongside the
`say`/`navigate`/targetless-`wait` early returns at ~compile.py:539-548), returning
`None`.** `_compiled_from` (which lives in **render.py:385-397**, not compile.py) and
`_instruction` (compile.py:74-86) need **no** change: both are already unreachable
for a slide because they sit behind `requires_target()` (render.py:405-406 returns
`action is None` early; compile.py gates `_instruction` the same way). The compiled
slot count still equals the number of steps; `_compiled_action_is_current` already
accepts `action is None` when `not requires_target()` (`render.py:405–406`), so no
`COMPILER_VERSION` bump is needed. `guidebot validate` and the scenario loader
(pure-pydantic) accept slide steps once the model changes land. Also teach the
verbose-compile helper `_short` (compile.py:114-125) to render a slide's title, else
`[n/m] slide:` prints an empty description.

## Multilingual

The on-screen slide text is single-language (baked into the shared picture). Only
narration switches across `config.audioTracks` (+ `translations` on the slide's
`say`). Consistent with the "one picture, many audio tracks" model.

## Files touched

- `guidebot_recorder/models/scenario.py` — `Slide` (incl. `hold`), the step field,
  exclusivity validator, `command_kind`/`requires_target`/`narration`, and the
  translations rule already keyed off `narration()`.
- `guidebot_recorder/models/config.py` — `IntroConfig`, `Config.intro`.
- `guidebot_recorder/slide/` — new `slide.js` + `slide.py` controller package.
- `guidebot_recorder/overlay/overlay.py` + `overlay/cursor.js` — `hide`/`show` +
  persistent `hidden` flag honored by `ensure`.
- `guidebot_recorder/chrome/chrome.py` + `chrome/chrome.js` — `hide`/`show` +
  persistent `hidden` flag honored by `ensure`.
- `guidebot_recorder/recorder/render.py` — `card_active` loop logic, slide painting
  before narration, `_render_step` slide `hold`, popup-close guard `+ "slide"`, intro
  bootstrap.
- `guidebot_recorder/recorder/compile.py` — a `kind == "slide"` early-return branch
  in `_compile_step` (→ `None`); `_short` renders the slide title for verbose logs.
  (`_instruction` and render.py's `_compiled_from` need no change — already gated by
  `requires_target()`.)
- scenario loader/validate as needed; **verify** hatchling ships `slide/*.js` (it
  already ships `cursor.js`/`chrome.js` with no explicit include — likely a no-op).

## Testing

- Slide model: requires ≥1 text field; rejects extra keys; `hold` default 2.5;
  mutually exclusive with other primary commands; `command_kind`/`requires_target`/
  `narration` behave; a `say`-less slide forbids `translations`, a `say` slide
  requires them.
- Compile emits `null` for a slide, never calls the Reasoner, preserves slot count.
- Overlay/Chrome: `hide()` then `ensure()` keeps the layer hidden (flag honored);
  `show()` restores it.
- Slide renderer: `show` mounts a `[data-guidebot-slide]` overlay leaving
  `[data-guidebot-cursor]`/page DOM intact; text is escaped (`textContent`).
- Render loop: a `slide` step paints the card before narration and hides
  cursor/chrome; a following `say` keeps the card; a following `navigate` **and a
  following `teach`** both dismiss it and restore cursor/chrome (the `teach`-after-slide
  case is the review #1 regression guard); a silent slide holds ~`hold` seconds; the
  popup path still validates. Intro replaces the white bootstrap when enabled, inert
  when off.
- Card-aware ensure discriminator: a `set_content` document rewrite under a `say`
  over a card is **repaired** (card re-mounted, shown-token present); a real
  `navigate` that wipes the card without rule-3 dismissal is **fail-loud**
  (`RenderError`, no shown-token on the fresh context).
- The card is **hit-testable**: a **click/hover** attempted while a card is still up
  fails loudly (Playwright hit-target check) rather than clicking through. (A card-up
  `enterText`/`waitFor` is not caught by hit-testing — it relies on rule 3's
  token-assert + dismissal; a test that a card wiped mid-`say` before a page-affecting
  step raises `RenderError` covers that path.)

## Recompile impact

- `IntroConfig` is render-only (no new step) → **no recompile**.
- Adding/removing/reordering `slide` **steps** changes the step count and the
  compiled slot alignment → **requires `guidebot compile`** (caught by the slot-count
  preflight, `render.py:472–473`).

## Cross-references

Independent of the cursor/typing/sound specs; combined in
`2026-07-15-demo-scenario-and-rollout-design.md`.
