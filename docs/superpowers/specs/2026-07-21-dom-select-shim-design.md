# DOM select shim — make dropdowns visible on camera

**Date:** 2026-07-21
**Status:** approved, ready to implement (revised after Fable 5 design review)

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

A second problem sits underneath. The target site enhances its selects with
**select2**, which hides the original `<select>` and renders its own list **in
the DOM**. Those dropdowns record perfectly — but `select:` cannot drive them:
the recorder only knows how to press arrow keys on the hidden original, so the
value changes with no visible interaction at all.

Note on what does *not* break today: select2 clips the original to 1×1 px, and
Playwright's visibility rule is a non-empty bounding box plus no
`visibility: hidden`, so `validate_compile_time`'s check
(`guidebot_recorder/resolver/validate.py:180-181`) **accepts** a select2 target.
The libraries that hide the original with `display: none` (Tom Select) are the
ones that fail to compile at all, with `not_visible`.

## Goal

Every `select:` step produces a **visible, on-camera dropdown interaction**: the
cursor travels to the control, the list unfurls **downward** in the DOM, the
cursor travels to the chosen option, and it is clicked.

Visual fidelity to the viewer's own browser is explicitly **not** a goal. A
dropdown that looks different from the real one but visibly opens downward is
better than an invisible one. (Stated by the commissioner when scope was agreed.)

## Non-goals

- Reproducing per-OS native dropdown chrome.
- Replacing controls the page already enhanced (select2 and friends). Their
  lists are DOM and already record correctly; we drive them, we do not rebuild
  them.
- Multi-select. `<select multiple>` and `<select size="2+">` already render as an
  in-page listbox with no OS popup, so they are left untouched.
- Search-as-you-type / AJAX-loaded option sets. Where a page's widget loads
  options remotely, driving it may fail; §8 makes that a hard error with a
  per-step escape hatch, not a silent degradation.

## Approach

### 1. New module `guidebot_recorder/selects/`

`selects.py` (Python controller) plus `selects.js` (the widget), the script read
with `importlib.resources.files(...)` and installed with
`context.add_init_script(...)`, structurally like `overlay/overlay.py:81-88`.

**Installation points must be added explicitly — the existing overlays do not
cover them.** `overlay`, `slide`, `desktop` and `chrome` are installed only in
render (`render.py:1987-2012`); `compile` creates a bare context with no init
script at all (`compile.py:230-236`). `selects.js` must be installed at every
context that drives a page through compile or render:

| Site | Function | Why |
| --- | --- | --- |
| `compile.py:230` | `run_compile_in_browser` | compile must see the same DOM as render |
| `render.py:1977` | render context | the feature itself |
| `session.py:248` | `replay_setup` | it calls `run_compile` (`session.py:255`) to replay the setup scenario; a setup scenario containing `select:` must behave identically |

Deliberately **not** installed at `session.py:304` (`check_logged_in`, a headless
health probe that drives no steps) nor `session.py:487` (`_manual_finish`, where
a human uses the browser and must get the real controls).

Installation lives in one helper (`selects.install_context(context, cfg)`) so
the three call sites cannot drift apart.

**Init-script ordering.** `render.py:1989-1995` documents a hard contract:
scripts that read the real `window.top` to decide their role must be registered
**before** `chrome.js`, which shadows `top`. `selects.js` needs that guard — it
must not shim anything in the shell document — so it registers alongside
`cursor.js`/`slide.js`, before `chrome.js`. It **does** run in every frame of the
site (including nested iframes), which is correct: a select in a nested iframe
needs shimming just as much.

### 2. Classification: raw vs already-enhanced

`settle_ms` (default 1000) after `load`, the script walks every `<select>` and
sorts it:

| Class | Detected by | Action |
| --- | --- | --- |
| **enhanced** | bounding box smaller than 8×8 px, **or** `display: none`, **or** `visibility: hidden`, **or** a known marker class (`select2-hidden-accessible`, `tomselected`, `chosen-select`) | leave alone — its list is already DOM |
| **raw** | has a real on-screen box | attach the shim |

The geometric test is library-agnostic on purpose: select2, Chosen and Tom Select
all **keep** the original `<select>` and merely hide it (select2 clips it to
1×1 px; Tom Select uses `display: none`). Marker classes are belt-and-braces, not
the primary signal. Side effect: selects the page deliberately keeps hidden are
skipped too.

A `MutationObserver` handles the dynamic cases, with the same `settle_ms`
debounce so the shim never wins a race against the page's own initialisation:

- select appears later → classify and shim;
- a shimmed select later gains a marker class or loses its box (late select2
  hydration, SPA route change) → **unshim** and reclassify as enhanced;
- a shimmed select leaves the document (framework re-render) → drop its overlay.

### 3. The shim is an overlay, not a wrapper — the page's DOM is not restructured

**The `<select>` is not moved, wrapped, or re-parented.** Its only mutation is a
`data-guidebot-shimmed` marker attribute. The shim consists of two elements
appended to the **site document's `<body>`**:

```
<div data-guidebot-select-button>   position:fixed, pinned to the select's rect,
                                    pointer-events:none, aria-hidden
<div data-guidebot-select-list>     position:fixed, opens downward, role=listbox
  <div data-guidebot-option data-guidebot-option-index="3">…
```

Every property here is load-bearing:

- **No DOM restructuring ⇒ frozen identities survive.** `capture_identity`
  (`resolver/identity_capture.py:140-151`) walks the *entire* composed ancestor
  chain, recording `(tag, role)` per ancestor, and `Identity.matches`
  (`models/identity.py:22-30`) compares the resulting `ancestry_digest` for
  strict equality. Inserting so much as one wrapper `<span>` would change that
  digest and fail `reuse_is_valid` (`render.py:2741`) for **every** cached action
  under it — `select:`, `click:`, `hover:`, `teach:` alike. An overlay changes no
  ancestry, so no identity moves, `identity_capture.py` needs no special-casing,
  and pre-existing `*.compiled.yaml` files keep working.
- **`pointer-events: none` on the button ⇒ the real `<select>` stays the hit
  target.** Playwright's click performs a hit-target check
  (`Recorder.click`, `recorder.py:94-103`); if the shim button intercepted
  pointer events, every click aimed at the select would fail. Instead the click
  lands on the select exactly as today, and `selects.js` listens for `mousedown`
  on shimmed selects, calls `preventDefault()` (which suppresses Chromium's
  native popup) and opens the DOM list. Keyboard (`ArrowDown` / `Enter` /
  `Space`) opens it too.
  Consequence worth having: `click:` and `teach:` steps that target a select now
  visibly unfurl the list instead of opening an invisible OS popup.
- **`<body>`-level, `position: fixed` ⇒ nothing can clip the list.** An
  `overflow: hidden` ancestor or an ancestor stacking context would otherwise
  bury it — which is exactly why select2 appends its own list to `<body>`.
  Z-index sits high but strictly **below** the cursor's `2147483647`
  (`overlay/cursor.js:18,271`), so the cursor is never painted under the list.
  (Only relevant in popup windows, where cursor and page share a document; in the
  main window the cursor lives in the shell above the iframe.)
- **Page CSS is untouched.** Structural selectors (`select + .hint`,
  `:nth-child`) keep matching, because the select's position among its siblings
  never changes.

Geometry is kept in sync by a single `requestAnimationFrame` loop that runs only
while at least one shim exists, re-pinning on rect change; `scroll` (capture
phase, so internally-scrolling containers count) and `resize` also trigger it.
This matters because scenarios scroll (`Recorder.scroll`, `recorder.py:194`).

**The list always opens downward** — a hard requirement. To keep it from
spilling past the bottom of the frame, `max-height` is clamped to the available
space (floor ~120 px) with internal scrolling, rather than flipping upward.

`<optgroup>` renders as non-clickable group headings; `disabled` options render
dimmed and are not clickable. Options are addressed by
`data-guidebot-option-index` (the index into `select.options`), never by their
label text: labels may repeat across optgroups and may contain quotes or
backslashes that would need CSS attribute escaping.

The button and list carry `aria-hidden="true"`, which keeps them out of the
resolver's candidate set — confirmed: `page_context.py:186` skips any node with
an `aria-hidden="true"` or `inert` ancestor. Also confirmed: the resolver's
visibility test does not consider opacity, so the shimmed select itself remains
a normal reasoner candidate.

**Readiness barrier.** `selects.js` exposes `window.__guidebot_selects_ready`
(resolved after the first classification pass). Compile and render await it
before resolving or driving a select step, so no step can race the `settle_ms`
debounce — otherwise compile could resolve at t=0.8 s against an unshimmed page
while render drives at t=5 s against a shimmed one.

### 4. Choreography — two beats instead of arrow keys

`Recorder._step_option_visibly` (`recorder.py:158-192`) is replaced by:

1. cursor glides to the control → ripple → click (SFX `click`) → the list
   unfurls;
2. hold `open_hold_ms` so the viewer can read the list, scroll the list
   internally to the wanted option if needed, glide the cursor to its row (hover
   highlight) → ripple → click (SFX `click`).

The same choreography serves both classes; only the targets differ:

| | beat 1 clicks | beat 2 clicks |
| --- | --- | --- |
| shim | the `<select>` itself (its `mousedown` opens the list) | `[data-guidebot-option-index="N"]` |
| page widget (select2 etc.) | the visible control associated with the hidden select | the node that appeared after opening whose trimmed text equals the option label |

**The association heuristic is pinned** (both here and in §6, one algorithm, not
two): given the hidden `<select>`, take the first of —

1. the element referenced by the select's `aria-controls` or `aria-owns`;
2. the element whose `aria-labelledby`/`aria-describedby` back-references the
   select's `id`;
3. the nearest following element sibling with a non-empty bounding box;
4. failing all three: `RenderError` per §8.

For beat 2, "appeared after opening" means: snapshot the site document before
the click, then match visible nodes added afterwards whose trimmed `textContent`
equals the option label; ties are resolved by document order.

Without an overlay — i.e. during `compile` — the existing direct path stays:
`locator.select_option(label=…)` sets the value with no animation. Compilation
is meant to be fast, not pretty.

`mode: native` (global or per-step) keeps **today's arrow-key stepping** under an
overlay — `_step_option_visibly` is retained, not deleted. An escape hatch must
never be *less* visible than what shipped before this branch; falling back to a
silent `select_option` would make the escape worse than the problem.

Compile does, however, **probe drivability** of
an enhanced widget (can the association heuristic resolve a visible control?)
and fails there, so an undriveable widget surfaces before a multi-minute render
is paid for.

**Re-selecting the current option**: when the clicked option is already
`select.selectedIndex`, the shim updates nothing and dispatches **no** `input`
or `change`. Native selects behave that way, and dispatching unconditionally
would make a page with an expensive `change` handler behave differently under
render than in real use — and differently from compile, which goes through
`select_option`.

### 5. Configuration

```yaml
config:
  selects:
    mode: shim            # shim (default) | native — global escape hatch
    settleMs: 1000        # wait for the page to enhance its own selects
    maxVisibleOptions: 8
    openHoldMs: 350       # pause after unfurling, before the cursor moves
```

Per-step override, for one stubborn widget in an otherwise fine scenario:

```yaml
- select:
    from: "lista województw"
    option: "Mazowieckie"
    mode: native          # optional; defaults to config.selects.mode
```

`config_hash` (`models/config.py:375-401`) follows the idiom already used for
`setup` (`config.py:398`): the projection gains `selects.mode` **only when it
differs from the default**. Existing scenarios keep their current hash and need
**no recompile** — and unlike the wrapper design this claim is actually true,
because §3 changes no ancestry, so `reuse_is_valid` still passes on a
`*.compiled.yaml` produced before this feature. Switching to `native` changes
the hash and forces a re-resolve, which is correct: it changes what the resolver
drives.

`settle_ms` and the cosmetic fields stay out of the hash, consistent with the
cosmetic chrome fields (`config.py:381-385`). This is safe only because identity
is ancestry-stable; it would not be under a wrapper design.

The per-step `mode` rides in the step, not the config, and therefore enters the
fingerprint through `compiled_from` like any other step content.

### 6. One validation change

`validate.py:180-181` rejects any target that is not `is_visible()`. For the
`select` action this relaxes to *"the control the user sees is visible"* — the
`<select>` itself, **or** the control resolved by §4's association heuristic.
Without it, `display: none` hiders (Tom Select) cannot be compiled at all.
`_is_native_select` (`validate.py:125-128`) is unchanged: the target remains a
real `<select>`.

## Error handling

If beat 2 cannot find a node matching the option label within the timeout, or
the association heuristic exhausts its four steps, the run **fails** with a
`RenderError` naming the option label and the control.

There is deliberately **no silent fallback** to `select_option()`. Falling back
would restore precisely the invisible magic this feature exists to remove, and
would do so unobservably: the run would succeed, the file would look fine, and
only a viewer would ever discover the step is unwatchable. This matches the
codebase's fail-loud posture everywhere else in `render.py`. The cost of a hard
failure is bounded by the two mitigations above: the per-step `mode: native`
override — which restores the pre-branch arrow-key animation, not silence — and
compile-time drivability probing.

## Testing

Test-first, in this order:

| Level | File | Covers |
| --- | --- | --- |
| JS | `tests/unit/selects/test_selects_js.py` (patterned on `tests/unit/overlay/test_cursor_js.py`) | raw select gets button + list and **is not re-parented** (ancestor chain identical before/after); 1×1-clipped and `display:none` selects untouched; `multiple` / `size>1` untouched; `mousedown` opens the list and suppresses the native popup; choosing an option sets `value` and fires `change`; re-choosing the current option fires **nothing**; list opens downward and clamps `max-height`; geometry re-pins after scroll; `MutationObserver` shims a late select, unshims one that gains a marker class, drops one removed from the document |
| Model | `tests/unit/models/` | `SelectsConfig` defaults and validation; per-step `Select.mode`; `config_hash` — default projection equals the pre-change hash, `native` differs |
| Resolver | `tests/unit/resolver/` | §6 relaxation: a `display:none` select with a visible associated widget validates; one with no resolvable control fails; the association heuristic's four steps in priority order |
| Recorder | `tests/unit/recorder/` | the two beats fire in order, two `click` SFX, the list scrolls to the option before the cursor glide; §8 raises `RenderError` with the option label; per-step `mode: native` takes the direct path |
| Back-compat | `tests/integration/` | **a `*.compiled.yaml` produced without the shim still renders with it** — the §5 no-recompile claim, and the single most expensive claim to get wrong |
| Integration | `tests/integration/fixtures/selects.html` + `tests/integration/test_selects_compile_render.py` | four controls — raw, fake-select2 (1×1 clipped), fake-Tom-Select (`display:none`), `multiple` — survive compile + render; the option ends selected; at click time the list is present and visible; a `click:` step targeting a shimmed select unfurls the list; a select inside a popup window works |

The fake select2 / Tom Select fixtures reproduce the *pattern* rather than
vendoring the libraries: a hidden original plus a sibling widget that opens a
`<body>`-level list on click.

## Documentation

- `docs/en/scenario-reference.md` and the Polish mirror — the `select:` section
  currently documents arrow-key stepping; add the per-step `mode`.
- The new `config.selects` block in the config reference.
- Docstrings that assert the list cannot be shown: `Select`
  (`models/scenario.py:53-62`), `Recorder.select` (`recorder.py:141-150`) and
  `Recorder.scroll` (`recorder.py:194-201`, which cites "native-select option
  lists" as content the resolver cannot target).

## Residual risks, accepted

- Page CSS that reacts to the *native* select's own appearance (e.g. styling
  `select:focus` with a visible outline) still applies to an element the viewer
  no longer sees; the shim paints over it.
- A page that repositions a select without any scroll/resize/mutation signal
  (e.g. a CSS animation) may lag by up to one frame before the overlay re-pins.
- `settle_ms` is a heuristic. A page that enhances its selects later than the
  configured window gets shimmed first and unshimmed by the observer — a brief
  double widget is possible on camera if a step fires in that window.
