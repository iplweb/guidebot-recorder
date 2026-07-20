# `target="_blank"` tabs — closing the window path

Date: 2026-07-20
Status: agreed design, ready for plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 1 / Layer 2 seam),
`2026-07-19-window-slide-transitions-design.md` (Spec C, slide transition)

## Why

A scenario author wants to record real sites that open links with
`<a href="..." target="_blank">`. Today that half-works in a way that is worse than
either working or failing: the click compiles fine, then the film silently ends up
on the new tab for the rest of its length, presented as a shrunken clone of the main
window.

This spec closes that path. It does **not** introduce a general multi-window model —
see [Follow-up](#follow-up).

## What already works (do not rebuild)

`compile.py:379-381` admits a new page on the strength of
`await candidate.opener() is active_page`. Playwright sets `opener()` for a
`target="_blank"` navigation exactly as it does for `window.open`, so a `_blank`
click already compiles and already records as a popup. **No new "open a window" step
is needed, and none will be added.**

The slide transition from Spec C (push-left on open, push-right on close,
`slideMs`) is likewise already implemented and frame-verified
(`tests/integration/test_popup_compile_render.py:444`). This spec reuses it; it does
not write a second transition.

## What is broken

### 1. Presentation collapses to the wrong branch

`_resolve_popup_crop` (`render.py:573-602`) is a three-level fallback. A `_blank` tab
fails all three:

- **`window.open` features** — the init script at `render.py:184-224` monkey-patches
  `window.open` to record `width=`/`height=`. A `_blank` link never calls
  `window.open`, so the patch never fires and the warning at `render.py:320-324`
  reports that no frame supplied a size. That warning blames the site for an omission
  the site never made.
- **content bbox** — declined at `render.py:374` (`paintsPage`): any real site paints
  a background on `html` or `body`. Even an unstyled tab is then declined by the
  degenerate gate (`render.py:489-493`, ratio `0.98`) because it genuinely fills the
  viewport.
- **cropdetect** — declined at `mux.py:356-357`: the detected rect *is* the full
  frame, so there is nothing to trim.

Result: `popup_crop = (None, "none")`. That is arithmetically correct — a tab really
is viewport-sized — but `transition: float` then scales a full viewport to
`scale: 0.85` (`config.py:174`) and insets it, which reads as a shrunken copy of the page rather than a
new window.

### 2. There is no way out

`PRIMARY_COMMANDS` (`scenario.py:17`) has no close or switch command;
`docs/en/scenario-reference.md:645` states this outright. Control returns to the main
window **only** when the popup closes as a side effect of an action performed on it
(`render.py:1788-1801`, `compile.py:400-414`); any other close raises
*"popup zamknął się asynchronicznie poza obsługiwaną akcją"*.

A `_blank` tab typically has no in-page close button — a real user presses Cmd+W. So
for the common case there is **no authorable way back at all**. The tab is then held
open to the end (`render.py:1844-1846`, `hold_open_at_end=True`) and every subsequent
step is filmed on it.

### 3. No coverage

`grep -rn "_blank" tests/ guidebot_recorder/` returns zero hits. Every popup test
drives `window.open`.

## Design

### A. Step model — `closeWindow`

`PRIMARY_COMMANDS` gains `close_window`, and `Step` gains:

```python
close_window: Literal[True] | None = Field(default=None, alias="closeWindow")
```

`Literal[True]` rather than `bool` is deliberate: `_exactly_one_command`
(`scenario.py:74`) tests `is not None`, so a `bool` would let `closeWindow: false`
count as a present command that does nothing. As typed, `closeWindow: false` is a
validation error.

Authoring:

```yaml
- teach: "Klikamy odnośnik Regulamin, który otwiera się w nowej karcie"
- say: "Przeczytaliśmy regulamin, wracamy."
- closeWindow: true
```

`optional: true` on this step is rejected with no new code: the step has no target,
so it falls through to the `raise` in `_optional_only_where_it_can_be_honoured`
(`scenario.py:83-99`).

Like `slide`, the step changes the step count and therefore **requires
`guidebot compile`**; render already fails loud on a stale sidecar. Following the
`slide` precedent it emits no `CachedAction` and adds no sidecar field, so
`COMPILER_VERSION` (`action.py:14`) does not move. *Confirm this during planning —
if the popup lifecycle turns out to need a compiled marker, the bump is in scope.*

### B. Semantics of `closeWindow`

- **compile** — closes `active_page`, sets `active_page = main_page`. This becomes a
  second *authorised* close path alongside the action-driven one; the rule at
  `compile.py:400-414` is not relaxed for anything else.
- **render** — closes the window, plays the Spec C push-right, and hands the cursor
  back through the existing `_prepare_main_after_popup_close` (`render.py:871-904`).
  **That funnel must be called with `restore_cursor_to`.** PR#20 gave it a
  `restore_cursor_to: tuple[float, float] | None = None` parameter (`render.py:876`)
  and its sole current call site passes `restore_cursor_to=popup.main_cursor_pos`
  (`render.py:1800`). Because the parameter defaults to `None`, a `closeWindow` path
  that merely "reuses the funnel" compiles, runs, and silently leaves the main
  window's cursor parked where the popup left it — reintroducing the bug PR#20 fixed,
  through a new door. The saved main-window position must be threaded through the
  close path explicitly; a test must pin it.
- `closeWindow` with no window open is a compile-time error, not a silent no-op.

### C. Presentation of a full-frame tab

The discriminator already exists and needs no new detection: `popup_crop is None`
after the full fallback chain means precisely "this window occupies the whole
viewport." When that holds:

1. the transition is forced to `slide` regardless of `config.popup.transition`,
   because `float` would inset a full viewport;
2. the address bar is **enabled** on that window;
3. the forcing is logged, and the warning at `render.py:320-324` no longer implicates
   the site when the popup did not originate from `window.open` at all.

**Which address bar.** The north star names *popup → shell conversion* as an open
problem owned by Spec B, hard because it collides with the `popup.opener() is page`
invariant and with site code holding the `window.open` WindowProxy. This spec does
**not** attempt it. It re-enables the **legacy in-DOM bar** that Spec A left available
for popups and that `bare_popups` merely suppresses. The trade-off is explicit: the
guarantee is heuristic, not structural — a site can paint into the bar's pixels.
Structural safety arrives only with the shell, in a later spec.

**Main implementation risk.** `bare_popups` is context-wide today
(`config.py:197-200`, `chrome.py:137-141`), and the comment at `render.py:620-632`
states the bar cannot be suppressed per-window because the init script is
context-level. This design needs that decision moved to per-window granularity.
**Plan this as a timeboxed spike first.** If per-window chrome proves materially more
expensive than it looks, the fallback is to ship (1) and (2)-without-the-bar and
raise the bar separately — full-frame slide alone is already a large improvement over
an inset clone.

## Testing

- **Unit, step model**: `closeWindow: true` accepted; `closeWindow: false` rejected;
  `optional: true` rejected; `closeWindow` alongside another primary command
  rejected.
- **Unit, presentation**: `popup_crop is None` forces `slide` and enables the bar;
  a sized `window.open` popup is unaffected in every transition mode.
- **Integration, first `_blank` coverage in the repo**: a local fixture page with
  `<a href="..." target="_blank">` — click, assert the new window slides in
  full-frame with an address bar; `closeWindow`; assert the push-right plays and
  subsequent steps are filmed on the main window again.
- **Integration, error**: `closeWindow` with no window open fails at compile.

Regression bar: `cut` and `float` for sized `window.open` popups stay byte-for-byte
unchanged, matching Spec C's acceptance discipline.

## Out of scope

- More than one popup per session — the invariant at `compile.py:373-385` and
  `render.py:1736-1737` stays.
- `switchWindow` / addressing windows by name — see Follow-up.
- Closing by emulating Cmd+W at the browser level.
- `_blank` inside a `when` branch — already unsupported and documented
  (`render.py:1303-1305`, `docs/en/scenario-reference.md:631-634`).
- Popup → shell conversion (structural address bar).

## Follow-up

The scenario language for addressing and switching windows was scoped out of the
implemented Spec C cut
(`2026-07-19-window-slide-transitions-design.md:43-45`) and is still absent. It gets
its own brainstorming cycle, because it carries real open questions: how windows are
addressed (names or indices), what happens to narration across a switch, and whether
`closeWindow` generalises or is subsumed.

Two blockers are already known and should be carried into that cycle:

- **Layer 1 orchestration, not the compositor.** Per the north star (lines 126-132),
  `_active_page` routes every step to the open popup and the one-popup invariant
  fails a second popup. Post-composition could concat N segments fine.
- **VFR frame emission.** A backgrounded page may emit no frames, so interleaving
  relies on "decode the last frame" behaviour that is currently implicit. This is the
  one genuine unknown in the model and deserves a spike before that spec is written.
