# Cursor: unclipped glow + arc motion

**Date:** 2026-07-20
**Status:** approved, ready to implement

Two independent defects in the synthetic cursor overlay
(`guidebot_recorder/overlay/cursor.js`):

1. The red glow is clipped, worst on the pointer's left edge.
2. Movement between two points is a mathematically straight line, which reads
   as machine-driven rather than hand-driven.

They share a file but nothing else. Part A ships on its own; Part B builds on
nothing from Part A.

---

## Part A — Unclipped glow

### Diagnosis

`cursor.js:137` sets on the cursor host element:

```js
setImportant(cursor, "contain", "layout style paint");
```

`contain: paint` clips all painting to the element's border box. The host is
sized exactly `CURSOR_WIDTH × CURSOR_HEIGHT` (default `34×46`, `cursor.js:128-129`),
while the halo is `drop-shadow(0 0 7px …)` (`cursor.js:105`) and therefore
spreads roughly 14px beyond the arrow's silhouette. Everything outside the
34×46 box is cut.

The left edge is hit hardest because the arrow path starts at `M2 1.5` inside
viewBox `0 0 24 32` (`cursor.js:107`): only `2/24 × 34 ≈ 2.8px` of slack. The
path's right extent is x=20 and its bottom extent y=24 of 32, leaving more
room on those sides — hence the asymmetry the user observed.

`overflow: visible` is already set on the SVG (`cursor.js:100`), so the SVG
itself is not the clipper.

### Change

`cursor.js:137` → `contain: layout style`.

`layout` and `style` provide the isolation this line was added for; `paint`
contributed nothing beyond the clip. No other change is needed.

### Test

Mount the cursor, read `getComputedStyle(host).contain`, assert `paint` is not
among the active containment keywords. Playwright + real Chromium, matching the
existing style of `tests/unit/overlay/test_cursor_js.py`.

**Chrome serializes the computed value as a shorthand.** `layout style paint`
comes back as the single keyword `content`, so a naive substring search for
`"paint"` passes against the *unfixed* code and tests nothing. The assertion
must expand the shorthands first (`content` → `layout paint style`,
`strict` → `layout paint size style`) and check membership in the expanded set.

Also assert `layout` and `style` remain present, so a future edit cannot
"fix" the glow by dropping containment altogether — isolation is why the
declaration exists.

---

## Part B — Arc motion

### Diagnosis

`moveTo` (`cursor.js:169-204`) animates via a single CSS transition on `left`
and `top`, both axes sharing one duration and one timing function
(`cubic-bezier(.45,.05,.25,1)`). Identical timing on both axes means the
traversed path is exactly the straight segment A→B; the easing only modulates
speed along that segment. No easing curve can bend a path built this way.

### Approach

Replace the CSS transition inside `moveTo` with a `requestAnimationFrame`
loop that walks a quadratic Bézier. Two helpers join the same IIFE. Nothing
outside `moveTo` changes.

**The Python side is untouched.** `_glide_duration` (`overlay.py:67-76`) still
derives duration from travel distance; `move_to` (`overlay.py:95-116`) still
calls `moveTo(x, y, duration)` and still receives a Promise that resolves
after `duration`. The JS API contract is unchanged.

### Path

Quadratic Bézier: `P0` = current rendered position, `P2` = target, `P1` =
segment midpoint displaced **perpendicular** to A→B.

Displacement magnitude is `bow × distance`, with sign and a small amplitude
modulation drawn from the seeded PRNG, then clamped at both ends:

- **Below `ARC_MIN_DISTANCE` (40px):** bow is zero. An arc across a 15px hop
  reads as a twitch, not as a hand.
- **Between `ARC_MIN_DISTANCE` and `ARC_RAMP_END` (140px):** the bow is scaled
  by a smoothstep ramp rising 0→1 across that span. A hard cutoff at 40px would
  jump from 0px of bow at 39px of travel to ~4.7px at 41px — a visible pop
  between two nearly identical moves. The ramp removes it.
- **Above `ARC_MAX_BOW_PX` (90px of displacement):** bow stops growing. A
  full-width screen sweep must not trace a half-circle.

So the displacement is `clamp(bow × distance × smoothstep(distance), 0, 90)`.

### Velocity profile

The existing `cursor.easing` knob is retained with unchanged semantics. The
difference is that `cubic-bezier(a,b,c,d)` is now parsed and evaluated in JS
(Newton-Raphson solver, ~25 lines) rather than handed to the CSS engine. The
solved value drives the Bézier parameter `t`.

Consequences:

- Every existing YAML keeps working untouched; the default
  `cubic-bezier(.45,.05,.25,1)` still means exactly what it meant.
- The approved motion feel — quick launch, long settle — comes out of the
  easing curve itself. No new timing knob is introduced.

If `cursor.easing` cannot be parsed as a `cubic-bezier(...)`, fall back to the
built-in default curve and log a single `console.warn` naming the bad value.
Do not throw: a cosmetic misconfiguration must not abort a render.

### Determinism

No `Math.random()` anywhere. The PRNG is mulberry32 seeded with a hash of the
rounded `(x0, y0, x1, y1)` quadruple.

Therefore: the same move in the same scenario always bows the same way, two
different moves bow differently, and the bow reproduces across machines and
across reruns. Re-rendering a scenario yields a frame-identical video.

### Loop

- Position is computed from `timestamp - t0`, **not** from a frame counter. A
  dropped frame must not desynchronize the animation from `duration`, which
  Python treats as authoritative.
- `transition` is set to `none` before the first frame. Left in place, the CSS
  transition would fight the per-frame writes.
- The final frame writes the target coordinates **exactly**. Without this the
  cursor settles a fraction of a pixel off and the subsequent click lands
  marginally off-target.
- The rAF handle lives in `state`. A new `moveTo` cancels any in-flight
  animation and starts from the currently *rendered* position — not from the
  abandoned target.

### Unchanged behaviour

- `duration === 0` still snaps instantly. This path is used by
  `_RESTORE_POSITION` (`overlay.py:20-27`) when the document is swapped.
- `state.x` / `state.y` are still assigned the target immediately on entry to
  `moveTo`. The post-swap restore reads that state and must receive the target,
  never an intermediate position.

### Config

One new field on `CursorConfig` (`guidebot_recorder/models/config.py:49-75`):

```python
bow: float = Field(default=0.12, ge=0)  # perpendicular arc depth, × travel distance
```

Threaded through the `appearance` dict in `overlay.py:46-65` as `bow`, read in
`cursor.js` as `CFG.bow ?? 0.12`. `0` disables arcing and restores straight-line
motion.

`ARC_MIN_DISTANCE`, `ARC_RAMP_END` and `ARC_MAX_BOW_PX` stay as JS constants.
They are not exposed to YAML until someone demonstrates a need.

`CursorConfig` is excluded from `config_hash`, so this addition does not force
a recompile of existing scenarios.

### Tests

Playwright driving real Chromium, matching `tests/unit/overlay/test_cursor_js.py`:

1. **Path bows.** Move (0,0)→(600,0) with `bow: 0.12`; sample mid-flight;
   assert `5 < |y| < 90`. The control point sits `min(0.12 × 600, 90) = 72px`
   off-axis, so a t≈0.5 sample lands near 36px; the window is wide enough to
   tolerate frame-timing jitter and narrow enough to fail a straight line.
2. **Determinism.** Run the identical move twice and assert the **sign** of the
   perpendicular deviation matches. Sign is what the seeded PRNG decides, and
   unlike a coordinate sample it does not depend on which frame the assertion
   happens to catch — comparing raw coordinates across two runs would be
   flaky in a real browser.
3. **Exact landing.** After `duration`, position equals the target exactly.
4. **Short moves stay straight.** Move (0,0)→(20,0); mid-flight `|y| ≈ 0`.
5. **`bow: 0` is straight.** Explicit config check that arcing can be disabled.
6. **`duration === 0` still snaps.**
7. **Bad easing falls back** rather than throwing.
8. **Regression (Part A):** computed `contain` excludes `paint`.

Mid-flight sampling must read the *rendered* geometry
(`getBoundingClientRect()` / computed `left`/`top`), not `state.x`/`state.y`,
which hold the target from the moment the move starts.

### Known trade-off — performance

Writing `left`/`top` every frame forces layout every frame; the CSS transition
could avoid that. During video capture this is a real cost.

The alternative is `transform: translate(x, y)`, which composites without
layout — but `left`/`top` are read as the source of truth in several places, so
switching would widen the change.

**Decision:** keep `left`/`top`, then measure. If frames drop during render,
`transform` becomes a separate follow-up rather than part of this work.
