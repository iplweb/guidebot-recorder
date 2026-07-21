# Guide: photograph the `select:` step with its list unfurled

**Date:** 2026-07-21
**Status:** approved, ready to implement
**Builds on:** `2026-07-21-dom-select-shim-design.md` (shipped to `main`)

## Problem

`guidebot guide` renders a compiled scenario as a step-by-step PDF. For a
`select:` step it photographs a **collapsed** control that already shows the new
value: the reader cannot tell the control is a dropdown, cannot see what it
offered, and is given no click target. It is the same complaint that produced the
DOM select shim for video — *"po prostu nic nie widać, dzieje się magia, ludzie
nie ogarną, że to select."*

The shim solved this for the recording. The PDF never got it: the guide context
installs no shim, and `guide/capture.py` drives the step with a bare
`locator.select_option(label=…)` and screenshots **after** the value has already
changed.

## Goal

A `select:` page in the PDF shows the option list **open**, with the option that
will be clicked marked the way a `click:` step marks its target.

## What the guide does today (verified)

- `guide/guide.py` `run_guide()` builds its own context: `browser.new_context`,
  `overlay.install_context`, `Chrome`, `install_shell`, `Recorder(...)`. There is
  **no** `install_selects`, no `Selects` controller and no readiness barrier
  anywhere on the guide path. The other three install sites are
  `recorder/compile.py`, `recorder/render.py` and `recorder/session.py`.
- `select:` is classified `"action"` and goes through the common action path in
  `guide/capture.py` `capture_pages()`: `reuse_failure` preflight →
  `recorder.point(action.target, ripple=False)` → the `elif act == "select":` arm
  → `select_option(label=…)` → `_screenshot(...)` **after the selection** →
  `apply_readiness`.
- Annotation: an arrow from the previous cursor plus a `selected` rectangle
  around the **collapsed control**.

## Approach

### 1. Install the shim in the guide context

Through the same shared funnel every other phase uses —
`selects.install_selects(context, cfg)` — registered next to the overlay, before
`chrome.js` (position is not load-bearing for this script; see
`Selects.install_context`). The controller is threaded into `capture_pages` so
the **readiness barrier** (`Selects.wait_ready`) can be awaited after each
navigation, exactly as compile and render do. `config.selects.mode: native`
makes `install_selects` return `None`, and the per-step override is read with the
existing `select_mode(step, cfg)` helper — no fourth copy of either rule.

The barrier is taken on the `navigate` arm, because the first frame of a new
document is photographed immediately and must show the same DOM compile resolved
against — and again on the `select:` arm, before the control is driven.

*Revised while merging `main`.* This section originally argued the navigate
barrier was enough: compile and render take it per step because a step may be
resolved by an LLM against a page snapshot, the guide resolves nothing, so "after
the document that will be photographed has loaded" looked like the only instant
that mattered. `main` has since changed what the barrier *means* — `wait_ready`
now awaits the classification pass owed **at the moment of asking**
(`selects.js::settled()`), precisely because `ready` never re-arms and a select
the page grows mid-run stays unclassified while every barrier reports ready. A
navigation-time answer therefore says nothing about a select added three steps
later, and driving that one finds a bare `<select>` with no list to unfurl. So
the guide takes it per `select:` step as well.

`Recorder.select` awaits `settled()` itself, but that is documented as the
*backstop* for a direct caller — a flat bound that knows nothing of `settle_ms`.
The guide is now a third production caller, so like compile and render it takes
the controller's bounded barrier first, which is also what wraps a wedged widget
in the step's own banner.

### 2. The `select:` arm becomes four phases

1. cursor to the control (unchanged `recorder.point`, which is also the
   optional-target-absent probe);
2. **open the list**, hold it open, scroll the wanted option into the list's own
   viewport, glide the cursor onto the option row and measure **the row's** rect
   and centre;
3. **screenshot — with the list open**;
4. commit the choice (click the row), read the select back, `apply_readiness`.

Phase 3 is the whole point, and it is why `res.box` from phase 1 cannot be used
for the annotation: by phase 3 the control may have moved, and for a
page-enhanced select the box was never the control the viewer sees in the first
place (a `display: none` original has no box at all). The geometry is re-derived
from the open list.

The pass already uses three different screenshot timings — `click`/`hover` before
the action, `type` after the fill, `select` after the selection — so moving one
action's frame is normal here, not a fight with a convention.

### 3. One owner for the choreography

Steps 2 and 4 are exactly what `Recorder`'s select machinery already does for the
video, for all three classes of control (shimmed, page-enhanced, natively-visible
listbox), including opening the list, locating the option row inside it, refusing
a `disabled` row, and reading the select back after the click. **None of it is
restated.** The predecessor branch was bitten twice by a rule with four
independent implementations; this one gets a hook, not a copy.

`Recorder.select` gains two keyword-only parameters:

- `on_revealed: Callable[[SelectReveal], Awaitable[None]] | None` — awaited
  **exactly once, immediately before the click (or `select_option`) that commits
  the choice**, on every path. That instant is the one the PDF wants: the list is
  open, the cursor is on the row, and nothing has been chosen yet.
- `ripple: bool = True` — a still capture wants a clean frame, so the guide
  suppresses the click ring and its flash. `capture.py` already passes
  `ripple=False` to `recorder.point` for the same reason.

`SelectReveal` carries the control's box and centre plus the option row's box and
centre. `row_*` is `None` when no list was unfurled — `mode: native`, where there
is nothing to reveal — and that is what tells the annotation builder to fall back
to today's shape.

Internally the three beat-2 methods are split into "find the row" and "click the
row", so the common tail (approach → `on_revealed` → click → `_confirm_selected`)
is written once. No rule about *which* row, or how to open which list, moves.

### 4. Annotations (owner-approved shape)

- a **click circle** on the option row — the same mark a `click:` step uses,
  because that is literally what the video's second beat does;
- the existing **`selected` rectangle**, but around the **control**, so the reader
  sees which field they are in;
- the **arrow** from the previous cursor position ending at the **option row**,
  not at the control.

The "previous cursor" carried into the next step becomes the option row's centre,
so the next arrow starts where the reader's eye was left.

`annotations_for` grows optional `row_box` / `row_center`; with neither (native
mode, or any control with no list) it produces exactly today's marks. One
function, one place where a `select` page's geometry is decided.

### 5. `mode: native` and the classes with no list

`mode: native`, globally or per step, keeps today's behaviour end to end: the
cursor travels to the collapsed control, the frame is taken there, the value is
set, and the annotation is the `selected` rectangle around the control. There is
no list to reveal and this must not become an error.

A `multiple` / `size > 1` listbox has no list to *unfurl* either — its rows are
already laid out — but it does have a row to mark, so it takes the full
annotation shape with the control rectangle around the listbox itself.

### 6. The preflight gap this exposes, and what it actually is

`validate_compile_time`'s `select` arm was deliberately loosened to accept a
page-enhanced select (select2 clipping the original to 1×1, Tom Select using
`display: none`), because render drives such a control through the page's own DOM
list. `reuse_failure` inherits that loosening and `reuse_failure` is the guide's
preflight — but the guide then ran `select_option` against the real `<select>`,
the very element the page hid.

**Measured, on `tests/integration/fixtures/selects.html`, with the guide's own
context recipe** (`scratch: probe_order.py`), for the three control classes:

| control | `reuse_failure` | `recorder.point` | `select_option` |
| --- | --- | --- | --- |
| raw `<select>` | `None` | box 220×21 | ok |
| select2 pattern (1×1 clipped) | `None` | box **1×1** | ok |
| Tom Select pattern (`display: none`) | `None` | box **`None`** | **`TimeoutError` after the full step timeout** |

So the call that raises first is `Locator.select_option`, on its actionability
wait ("waiting for element to be visible and enabled"), in English, with a
Playwright call log and no mention of the scenario. `reuse_failure` passes and
`point` does not raise at all — it silently degrades to a box-less approach,
which is also why that page's annotation rectangle would have been empty even if
the value had been set. The select2 row is not an error but is no better as
output: a 1×1 annotation rectangle around an invisible control.

**The fix is the feature.** Once the guide drives page-enhanced widgets the way
render does, all three rows work and produce a frame with the list open; the
`TimeoutError` cannot happen because the hidden original is never the click
target. An integration test pins exactly that (`selects.html`'s Tom Select
control, driven through the guide).

**Where it genuinely cannot be driven**, the refusal is up front and Polish:

- *the page hid the `<select>` and nothing visible stands in for it* — this is
  the one thing `not_visible` can mean for a `select` action, because
  `validate_compile_time`'s select arm reaches it only through
  `user_visible_control() is None`. The guide replaces the generic "cel jest
  niewidoczny" with the recorder's own situation-naming sentence, obtained from
  `Recorder.diagnose_select` — the same `_no_control_error` render raises, not a
  second wording. It fires in the preflight, before the cursor moves and before
  any frame is taken.
- *the select is on screen but has no DOM list to unfurl* (a marker class the
  shim honours, `mode: native` pinned on it, no shim in this context) — already
  refused by `Recorder.select` before it approaches or clicks anything, with a
  message that names `mode: native`.

One deliberate deviation from the commissioning brief, on evidence: the *first*
of those two messages does **not** point at `mode: native`, because that advice
would be unfollowable. `validate_compile_time` is mode-blind, so a hidden select
with no visible stand-in is refused under `native` exactly as under `shim` — it
cannot be compiled at all, and the existing
`test_undriveable_widget_fails_loudly_instead_of_setting_the_value` pins that.
`_no_control_error` already draws this distinction: its `hidden` branch offers no
escape hatch and its other two branches do. Repeating the offer here would
recreate precisely the trap shim-spec §6 called out — advice impossible to follow
because validation rejects the target under every mode.

The user-facing wording of the two shared messages loses its "na filmie" framing
("nie da się pokazać **na filmie** wyboru opcji…") in favour of medium-neutral
phrasing, since they are now raised by a phase that produces a PDF.

### 7. `optional: true` and a vanished option (added while merging `main`)

`main` grew an optional-step skip for a select whose option is gone —
"pomijam: brak żądanej opcji" — wrapped around the `select_option` call this
branch deletes. Losing it to a merge resolution would have deleted a feature, so
it is restored under the new choreography, but *not* as "an optional select may
fail quietly": every way a `select:` step can fail now arrives as the same
`SelectDriveError`, and catching that type wholesale would turn a click that did
not take, a widget with nothing to unfurl and a shim removed mid-step into
silently dropped PDF pages — the exact unwatchable-but-successful output §3 and
the constraints below exist to make impossible.

So `SelectDriveError` gains a machine-readable `reason`:

- `OPTION_MISSING` — the control does not carry that label. Established by
  reading the real `<select>`'s options, which is what
  `validate.reuse_failure(option=…)` already asks at preflight for the
  mandatory steps it does get to check. Raised from all four paths: the shimmed
  list, the listbox, the page widget (whose underlying `<select>` is consulted
  *before* the option wait, so "not on offer" is told apart from "the widget did
  not draw it"), and the two listless direct sets, which previously left the
  miss to `select_option`'s actionability timeout.
- `UNDRIVABLE` — everything else, and the default, so a raise site that says
  nothing stays loud.

Only `OPTION_MISSING` on an `optional: true` step skips. This is also strictly
narrower than what `main` shipped: `main` caught any `PlaywrightError` from
`select_option`, which included the actionability timeout §6 measured on a
`display: none` Tom Select — i.e. "this widget cannot be driven at all" was being
skipped as "the option is not there".

## Constraints carried over from the shim

- The `<select>` is never re-parented, wrapped or moved — element identity hashes
  the full composed ancestor chain, and every frozen `*.compiled.yaml` depends on
  it. This branch adds no DOM manipulation of its own; it only installs the
  existing widget in one more context.
- The shim button stays `pointer-events: none`; the real `<select>` remains the
  hit target.
- No on-camera or on-page path may return a silent no-op. There is no fallback to
  `select_option()` on the reveal path: it would restore exactly the invisible
  value change this exists to remove, and the PDF would look fine while being
  useless. Every path still ends at `_confirm_selected`.

## Testing

Test-first, in this order. The central assertion is deliberately not "a code path
ran" or "the final value is right" — this lineage has repeatedly been bitten by
tests whose names claim more than their assertions check.

| Level | File | Covers |
| --- | --- | --- |
| Annotate | `tests/unit/guide/test_annotate.py` | a `select` with row geometry yields a **click circle on the row**, a `selected` rectangle on the **control**, and an arrow **ending at the row**; with no row geometry it yields today's marks byte for byte |
| Capture | `tests/unit/guide/test_capture.py` | the frame is taken **while the list is open** — i.e. between `on_revealed` and the commit — and never again; `prev_cursor` for the next step is the row centre; the readiness barrier is awaited after navigation; `mode: native` keeps today's shape; the `not_visible` preflight verdict for a `select` becomes the recorder's own diagnosis; an `optional:` step skips **only** `OPTION_MISSING` and still fails on `UNDRIVABLE` (§7) |
| Recorder | `tests/unit/recorder/test_recorder_select.py` | `on_revealed` fires exactly once, before the commit, on all four paths (shim, page widget, listbox, native), carrying the row's real rect; `ripple=False` suppresses the ring; and one `reason` case per raise site (§7), including the mirror pair "the widget draws the row the `<select>` lost" / "the `<select>` has the option the widget never drew" |
| Guide wiring | `tests/unit/guide/test_guide.py` | `run_guide` installs the shim through `install_selects` and hands the controller to `capture_pages`; `mode: native` installs nothing |
| **Integration** | `tests/integration/test_guide_select_reveal.py` | **the central test**: compile + guide `selects.html`, then assert on the produced PNG that the pixels under the click annotation's centre show the **open list** and not the page underneath, and that the annotation's centre falls **inside the option row's rect**. Also: the `display: none` Tom Select control is driven successfully (the `TimeoutError` of §6 is gone), and `mode: native` still produces a page |

The PNG is read with a ~40-line pure-stdlib decoder (`zlib` + `struct`) in the
test helper; the project has no image dependency and does not gain one for this.

## Documentation

- `docs/en/pdf-guide.md` and `docs/pl/pdf-guide.md`: the "`select` shows no
  expanded dropdown" limitation is deleted and replaced by a description of the
  three marks, plus the `mode: native` fallback.
- `2026-07-21-dom-select-shim-design.md` §1: the install-sites table gains the
  guide context, and the "deliberately not installed" list keeps only the two
  session probes it always meant.
