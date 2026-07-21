# DOM select shim — make dropdowns visible on camera

**Date:** 2026-07-21
**Status:** approved, ready to implement

## Problem

A native `<select>` draws its option list as an **operating-system popup**, not
as DOM. Playwright's screencast records the page, so the list is simply absent
from the recording: the value on the collapsed control changes and nothing
explains why.

`Recorder.select` (`guidebot_recorder/recorder/recorder.py:141-192`) already
concedes this in its own docstring — *"A native select's option list is drawn by
the OS, so no browser-automation tool can unfurl or screenshot it"* — and works
around it by **stepping the value with arrow keys** (`ArrowDown` every 140 ms,
`recorder.py:184-189`). The viewer sees the field flicker through values with no
list, no cursor travel, and no indication that this control is a dropdown at
all. In the words of the person commissioning this: *"po prostu nic nie widać,
dzieje się magia, ludzie nie ogarną, że to select."*

A second, sharper problem sits underneath. The target site enhances its selects
with **select2**, which hides the original `<select>` and renders its own list
**in the DOM**. Those dropdowns record perfectly — but `select:` cannot drive
them, because the recorder only knows how to press arrow keys on the hidden
original, and `validate_compile_time` (`guidebot_recorder/resolver/validate.py:186`)
insists the target be a *visible* element.

## Goal

Every `select:` step produces a **visible, on-camera dropdown interaction**: the
cursor travels to the control, the list unfurls **downward** in the DOM, the
cursor travels to the chosen option, and it is clicked.

Visual fidelity to the viewer's own browser is explicitly **not** a goal. A
dropdown that looks different from the real one but visibly opens downward is
better than an invisible one. (Stated by the commissioner when the scope was
agreed.)

## Non-goals

- Reproducing per-OS native dropdown chrome.
- Replacing controls the page already enhanced (select2 and friends). Their
  lists are DOM and already record correctly; we drive them, we do not rebuild
  them.
- Multi-select support beyond what exists today. `<select multiple>` and
  `<select size="2+">` already render as an in-page listbox with no OS popup,
  so they are left untouched.
- Search-as-you-type / AJAX-loaded option sets. Where a page's widget loads
  options remotely, driving it may fail; §8 defines that as a hard error, not a
  silent degradation.

## Approach

### 1. New module `guidebot_recorder/selects/`

Same shape as the existing injected surfaces (`chrome/`, `overlay/`, `slide/`):
`selects.py` (Python controller) plus `selects.js` (the widget), the script read
with `importlib.resources.files(...)` and installed with
`context.add_init_script(...)` — mirroring `overlay/overlay.py:81-88` and
`chrome/chrome.py:163-172`.

The script is installed in **both `compile` and `render`**. This is load-bearing,
not tidiness: `render.py:2741` re-runs `reuse_is_valid()`, which re-validates the
cached target and compares the frozen identity against the live DOM. A shim that
existed only at render time would present the resolver with a DOM the compiler
never saw, failing every select step with *"niezgodna tożsamość — uruchom
`compile --force`"*.

### 2. Classification: raw vs already-enhanced

`settle_ms` (default 1000) after `load`, the script walks every `<select>` and
sorts it into one of two classes:

| Class | Detected by | Action |
| --- | --- | --- |
| **enhanced** | bounding box smaller than 8×8 px, **or** `display: none`, **or** a known marker class (`select2-hidden-accessible`, `tomselected`, `chosen-select`) | leave alone — its list is already DOM |
| **raw** | has a real on-screen box | replace with the shim |

The geometric test is library-agnostic on purpose: select2, Chosen and Tom Select
all **keep** the original `<select>` and merely hide it (select2 clips it to
1×1 px; Tom Select uses `display: none`). The marker classes are a cheap
belt-and-braces addition, not the primary signal.

A useful side effect: selects the page deliberately keeps hidden (collapsed
sections, `display: none` templates) are also skipped.

A `MutationObserver` catches selects added later, applying the same `settle_ms`
debounce so the shim never wins a race against the page's own initialisation.

### 3. Shim structure — the original `<select>` stays, and stays *visible*

For each raw select the script builds:

```
<span data-guidebot-select>                    <- takes the select's place in flow
  <select …>…</select>                         <- opacity:0; position:absolute; inset:0
  <button data-guidebot-select-button>         <- what the viewer sees
  <div data-guidebot-select-list role=listbox> <- the dropdown
    <div data-guidebot-option="Mazowieckie" role=option>…
```

Design constraints this satisfies:

- **The original stays in the DOM with a non-empty box.** `validate.py:186`
  requires `locator.is_visible()` and `validate.py:188` requires
  `tagName === "select"`. Playwright treats `opacity: 0` as *visible* (it checks
  for a non-empty bounding box and the absence of `visibility: hidden`), so the
  whole validation and `capture_identity` layer — role `combobox`, accessible
  name — keeps working **unchanged**.
- **The original stays the source of truth.** Page JS, form validation and
  submit see no difference. On choosing an option the shim sets `select.value`
  and dispatches bubbling `input` and `change` events, exactly as select2 does.
- **Layout barely moves.** The button copies font, box metrics and border radius
  from the original's `getComputedStyle`.
- **Semantic targets survive.** `models/target.py` shows targets are semantic
  (`role` / `text` / `label` / `testid`), never CSS paths with `nth-child`, so
  inserting a wrapper breaks nothing: the select keeps its `id`, its role, its
  accessible name and its ancestors (both `<label for=…>` and a wrapping
  `<label>` keep resolving).

The shim's own button and list carry `aria-hidden="true"` so they never appear
as candidates in the resolver's accessibility snapshot
(`resolver/page_context.py`) and cannot make a `RoleTarget` ambiguous.

**The list always opens downward** — a hard requirement from the commissioner.
To keep it from spilling past the bottom of the site iframe, `max-height` is
clamped to the available space (floor ~120 px) with internal scrolling, rather
than flipping the list upward.

`<optgroup>` renders as non-clickable group headings; `disabled` options render
dimmed and are not clickable.

### 4. Choreography — two beats instead of arrow keys

`Recorder._step_option_visibly` (`recorder.py:158-192`) is replaced by:

1. cursor glides to the control → ripple → click (SFX `click`) → the list
   unfurls;
2. hold `open_hold_ms` so the viewer can read the list, scroll the list
   internally to the wanted option if needed, glide the cursor to its row (hover
   highlight) → ripple → click (SFX `click`).

The same choreography serves **both** classes; only the elements clicked differ:

| | beat 1 clicks | beat 2 clicks |
| --- | --- | --- |
| shim | `[data-guidebot-select-button]` inside the wrapper | `[data-guidebot-option="…"]` |
| page widget (select2 etc.) | the visible control associated with the hidden select — via `aria-controls` / `aria-owns`, else the nearest visible sibling | a node that **appeared after opening** whose trimmed text equals the option label (select2 appends its list to `<body>`) |

Without an overlay — i.e. during `compile` — the existing direct path stays:
`locator.select_option(label=…)` sets the value with no animation. Compilation
is meant to be fast, not pretty.

### 5. Configuration

```yaml
config:
  selects:
    mode: shim            # shim (default) | native — escape hatch
    settle_ms: 1000       # wait for the page to enhance its own selects
    max_visible_options: 8
    open_hold_ms: 350     # pause after unfurling, before the cursor moves
```

`config_hash` (`models/config.py:375-401`) follows the idiom already used for
`setup` (`config.py:398`): the projection gains `selects.mode` **only when it
differs from the default**. Existing scenarios therefore keep their current hash
and need **no recompile** — the shim enters both `compile` and `render`, so both
see the same DOM, and `reuse_is_valid` still passes because the `<select>` never
leaves the DOM and keeps its role and accessible name. Switching to `native`
changes the hash and forces a re-resolve, which is correct: it changes what the
resolver sees.

The cosmetic fields (`max_visible_options`, `open_hold_ms`) and `settle_ms` stay
out of the hash, consistent with how the cosmetic chrome fields are treated
(`config.py:381-385`).

### 6. One validation change

`validate.py:186` rejects any target that is not `is_visible()`. For the
`select` action this relaxes to *"the control the user sees is visible"* — the
`<select>` itself, **or** its shim wrapper, **or** the page widget associated
with it. Without this, libraries that hide the original with `display: none`
(Tom Select) fail to compile at all. `_is_native_select` (`validate.py:125-128`)
is unchanged: the target remains a real `<select>`.

## Error handling

If beat 2 cannot find a node matching the option label within the timeout —
an unusual widget, or options loaded by AJAX only after typing — the render
**fails** with a `RenderError` naming the option label and the control.

There is deliberately **no silent fallback** to `select_option()`. Falling back
would restore precisely the invisible magic this feature exists to remove, and
would do so unobservably: the run would succeed, the file would look fine, and
only a viewer would ever discover the step is unwatchable. The escape hatch is
explicit: `mode: native`.

## Testing

Test-first, in this order:

| Level | File | Covers |
| --- | --- | --- |
| JS | `tests/unit/selects/test_selects_js.py` (patterned on `tests/unit/overlay/test_cursor_js.py`) | raw select gains wrapper + button + list; a select clipped to 1×1 and one with `display: none` are **untouched**; `multiple` / `size>1` untouched; choosing an option sets `value` and fires `change`; the list opens downward and clamps `max-height` near the viewport bottom; `MutationObserver` picks up a select added after `settle_ms` |
| Model | `tests/unit/models/` | `SelectsConfig` defaults and validation; `config_hash` stability — the default projection equals the pre-change hash, `native` differs |
| Recorder | `tests/unit/recorder/` | the two beats fire in order, two `click` SFX, the list scrolls to the option before the cursor glide |
| Integration | `tests/integration/fixtures/selects.html` + `tests/integration/test_selects_compile_render.py` | a page with three controls (raw, fake-select2, `multiple`) survives compile + render; the option ends up selected; at click time the list is present and visible in the DOM |

The integration fixture's fake select2 reproduces the pattern rather than
vendoring the library: an original `<select>` clipped to 1×1 with
`select2-hidden-accessible`, plus a sibling widget that opens a `<body>`-level
list on click.

## Documentation

- `docs/en/scenario-reference.md` and the Polish mirror — the `select:` section
  currently documents arrow-key stepping.
- Docstrings that assert the list cannot be shown: `Select`
  (`models/scenario.py:53-62`), `Recorder.select` (`recorder.py:141-150`) and
  `Recorder.scroll` (`recorder.py:194-201`, which cites "native-select option
  lists" as content the resolver cannot target).
- The new `config.selects` block in the config reference.
