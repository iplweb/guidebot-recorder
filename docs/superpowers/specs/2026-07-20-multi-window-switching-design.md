# Multi-window switching — the scenario language for addressing windows

Date: 2026-07-20
Status: agreed design, ready for plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 1 / Layer 2 seam),
`2026-07-19-window-slide-transitions-design.md` (Spec C — the slide transition),
`2026-07-20-blank-tab-window-design.md` (`closeWindow`, full-frame tab presentation)

## Why

The north star sequenced Spec C as "multi-window transitions … **and whatever
scenario-language surface is needed to address windows**" (lines 102-104). The
implemented cut delivered only the compositor half and scoped the language out
explicitly (`2026-07-19-window-slide-transitions-design.md:43-45`). This spec
delivers the missing half: an author can open several windows, jump between them,
and have each jump read legibly in the film.

The target shape, in full:

```yaml
- teach: "Klikamy Regulamin (nowa karta)"
  opensWindow: regulamin
- say: "Tu jest paragraf 5."
- switchWindow: main
- teach: "Zaznaczamy zgodę"
- switchWindow: regulamin
- say: "A tu paragraf 6."
- closeWindow: true
```

## Measured facts (spike, 2026-07-20)

A throwaway experiment settled the one open technical question. Recording the
numbers here because the scripts were scratchpad-only and are gone.

Playwright **1.61.0**, Chromium **149.0.7827.55**, context set up as
`render.py:1415-1421` (`record_video_dir`, `record_video_size` 640×480).

**1. Recordings are CFR, not VFR, and never go dark.** Interleave run
(main → tab → main → tab, ~2 s each):

| | duration | frames | avg fps | max inter-frame gap |
|---|---|---|---|---|
| main | 9.960 s | 249 | 25.00 | 0.0400 s |
| tab | 7.920 s | 198 | 25.00 | 0.0400 s |

100 % of gaps below 0.05 s; the maximum equals exactly one frame interval, i.e.
there is no gap anywhere. A page painting nothing for 3 s, and a page genuinely
frozen via CDP `Page.setWebLifecycleState: "frozen"`, both kept recording a flat
25 fps through the dead interval, padded with duplicates of the last painted frame.

**Consequence:** the compositor needs no hold filter, no gap-filling, and no
reliance on decoder "last frame" semantics. Every window's recording is a dense
25 fps timeline for its whole lifetime.

**2. Focus is not a signal.** `bring_to_front()` does not background anything — a
popup is a separate OS window, not a tab. Probed in both headless and headed mode,
both pages simultaneously reported `visibilityState: visible`, `hidden: false`,
`hasFocus: true`, at every phase. **Which window is "active" must be tracked
explicitly by the orchestrator; it can never be read from the browser.**

**3. The real alignment hazard is the per-page PTS origin.** Each recording's PTS 0
is the moment *that page* was created: main spanned 9.96 s from context start, the
tab 7.92 s from its own creation at t≈2.11 s. Composited naively they misalign by
~2.1 s. `compose_popup_video` already performs this offsetting for a single popup;
it must generalise to N.

## Design

### A. Scenario language

**`opensWindow: <name>` — a modifier, not a command.** It rides on the click step
that opens the window, exactly as `optional` rides on a step today, and does **not**
join `PRIMARY_COMMANDS`: the step is still a click.

```python
opens_window: str | None = Field(default=None, alias="opensWindow")
```

Validation happens in two places, because the model cannot know what a `teach`
resolves to:

- **model** — allowed only on steps that *may* produce a click, i.e. `teach` and
  `click`; on anything else it is an error, mirroring
  `_optional_only_where_it_can_be_honoured` (`scenario.py:83-99`). Names must be
  unique within a scenario, and `main` is reserved.
- **compile** — once the step resolves, the action must actually be a `click`; a
  `teach` that resolves to a hover or a type while carrying `opensWindow` is an
  error. This mirrors the existing guard at `action.py:97-100`, which already
  restricts `opens_popup` to click actions.

**`switchWindow: <name>` — a new primary command.** Joins `PRIMARY_COMMANDS`
(`scenario.py:17`). The target must be a known and currently-open window. Switching
to the already-active window is an error, not a no-op — a step that does nothing is
a bug in the scenario, and failing loud is this codebase's established habit.

**`closeWindow: true` — unchanged from the `_blank` spec.** It closes the **active**
window only. Closing a *named* window from a distance was considered and rejected:
a window closing while the viewer is looking at a different one reads as something
vanishing off-screen. Requiring `switchWindow` first guarantees the close is always
visible. This also keeps `closeWindow` byte-identical to the already-committed spec.

### B. Layer 1 — orchestration (the substance of this spec)

`_active_page` becomes a **window registry** holding, per window: name, `Page`,
open/closed state, creation offset against the main clock, open-order depth, and
**saved cursor position**. One entry is pre-seeded as `main`.

The cursor field is not optional bookkeeping. `Overlay.pos` is shared across pages,
so a window's cursor position has to be saved on the way out and restored on the way
back; PR#20 established exactly this for the single-popup case via
`popup.main_cursor_pos` and `_prepare_main_after_popup_close(..., restore_cursor_to=)`
(`render.py:876`, call site `render.py:1800`). With `switchWindow` that save/restore
is needed on **every switch**, not only on open and close. A registry without this
field would land the cursor at the wrong place on every jump back.

- Steps route to `registry.active`. Per measured fact 2, focus is never consulted.
- The "one popup per session" invariant is removed from both compile
  (`compile.py:373-385`) and render (`render.py:1736-1737`).
- `_unexpected_pages` (`compile.py:515-524`, `render.py:635-647`) changes meaning
  from "anything besides main and the one popup" to "any page not in the registry".
- The `opener()` admission check generalises: a new page's opener must be the
  **active** page, and the page is bound to the name declared by `opensWindow` on
  the step that produced it. A new page appearing with no `opensWindow` declared is
  an error — this preserves today's fail-loud posture toward stray windows.

### C. Layer 2 — compositor

`compose_popup_video` handles exactly one interval (`opened_at`/`closed_at`). It
generalises to an ordered **segment list** — `(window, start, end)` over the main
clock — with a transition between consecutive segments.

- Each window's source is shifted by its own creation offset (measured fact 3).
  This is the one genuinely new correctness requirement in the compositor.
- No gap-filling, per measured fact 1.
- **Push direction from depth.** `main` has depth 0; each newly opened window takes
  the next depth. Switching to a deeper window pushes left; to a shallower one,
  right. Across an interleave the motion then carries "deeper / back out" rather
  than being decoration. `_compose_slide` from Spec C is reused; only the direction
  selection is new.

### D. Compiled artifact

`opens_popup: bool` (`action.py:92`) can no longer carry the needed information — it
must name the window. Its validator (`action.py:97-100`) generalises with it.

This changes the sidecar format, so **`COMPILER_VERSION` moves 2 → 3**
(`action.py:14`), invalidating existing compiled artifacts. The `_blank` spec
deliberately avoided a bump; here it is unavoidable and should be taken knowingly.

## Sequencing

`2026-07-20-blank-tab-window-design.md` is a strict subset and ships **first**: it is
small, independent, and needs no version bump. This spec builds on the `closeWindow`
step and the full-frame presentation rule that one establishes.

## Testing

- **Registry, unit**: duplicate names rejected; `main` reserved; `switchWindow` to an
  unknown, closed, or already-active window rejected; `opensWindow` on a non-click
  step rejected; a stray page with no `opensWindow` rejected.
- **Compositor, unit**: an N-segment timeline; push direction per depth in both
  directions; per-window PTS offset applied; back-to-back switches with no step
  between them.
- **Integration**: the interleave scenario from Why — main → tab → main → tab →
  close. Assert each segment is filmed on the correct window, that pushes run in the
  correct direction, and that the tab's content at the second visit has advanced
  along the **recording clock** `t_real` (the recording never stopped). Name the
  clock explicitly in the assertion: with `hold_frame_for_narration` enabled, film
  time and recording time diverge by the running freeze sum, so "elapsed wall-clock"
  alone is ambiguous.
- **Regression**: single-popup `cut` / `float` / `slide` output unchanged, matching
  Spec C's byte-for-byte discipline.

## Interaction with `hold-frame-for-narration`

That design (`2026-07-20-hold-frame-for-narration-design.md`, merged in PR#20) and
this one meet on the same function, so the order they land in matters.

- **It declares as fixed what this spec rewrites.** Its pipeline diagram marks
  `compose_popup_video` as "existing code, UNCHANGED", and its §2 states that
  `opened_at`/`closed_at`, the encoder start-up compensation, and the
  `crop`-before-`scale` ordering all stay untouched. §C above turns that single
  interval into an N-segment list. These are not logically contradictory, but
  **whichever lands second invalidates the other's stated premise**, and the second
  plan must re-state the baseline rather than inherit it.
- **The stage order protects per-window offsets — keep it.** Hold-frame applies
  `apply_time_edits` *after* composition, so the per-window creation offsets of
  measured fact 3 are computed wholly in `t_real` and freezes land on an
  already-composited frame. Freezes therefore cannot desynchronise window alignment.
  This is the safe ordering and it is load-bearing here: editing before composing
  would make every window's offset drift by the running freeze sum.
- **Its consumer inventory under-counts once windows are plural.** Hold-frame names
  three `anchor` consumers — narration offsets, `sfx_events`, and popup
  `opened_at`/`closed_at` — and argues that one mapping function makes forgetting a
  consumer impossible. The argument holds; the enumeration does not. This spec
  replaces the third consumer with N segment boundaries plus N creation offsets from
  the registry.
- **This spike answers an open question of theirs.** Hold-frame §4 flags that
  "Playwright writes variable-frame-rate WebM, so 'fps' is not a single number" and
  requires verification during implementation. Measured fact 1 settles it: the
  recordings are CFR at exactly 25.00 fps with a maximum inter-frame gap of one
  frame interval. Their proposed `fps=N` guard ahead of `trim` may be unnecessary.

## Out of scope

- The iframe shell for windows (a structural address bar rather than the in-DOM one)
  — still the north star's open problem, owned by a later spec.
- Windows opened by anything other than a click.
- `opensWindow` inside a `when` branch — pop-ups there are already unsupported
  (`render.py:1303-1305`).
- More than one window opened by a single click.

## Risks

1. **Compositor generalisation from one interval to N** is the largest single piece.
2. **`COMPILER_VERSION` 2 → 3** invalidates every existing sidecar.
3. **N concurrent windows widen the "unexpected page" surface**, which is where
   flaky tests would come from if the registry's admission rules are loose.
4. **Unverified: whether compile reliably drives N pages at once.** The spike
   measured recording behaviour, not compile orchestration. Plausible, unproven —
   worth an early check in the plan rather than an assumption.

## Doc correction shipped with this spec

The north star's parenthetical (lines 126-132) states that "a backgrounded page may
emit no frames under VFR, so any dimmed-backdrop/interleave plan relies on 'decode
the last frame' behavior — currently implicit." Measured fact 1 disproves it. The
note is replaced with the measurement, so later specs do not inherit a false
constraint.
