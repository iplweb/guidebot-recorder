# Spec C — window slide transitions (the "stage", v2)

Date: 2026-07-19
Status: approved after fable self-review + ffmpeg probe (corrections applied), ready for plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 2 — the stage), Spec A
(iframe shell) and Spec B (floating popup — the compositor stage).

First cut of the north-star's multi-window layer: an animated **slide/push**
transition when the film switches to and from a window, instead of the hard cut
(original) or the floating overlay (Spec B). Matches the ask: "opening a new window
and switching to it is a push animation, switching windows."

> Revision note: integrates a fable review with a working ffmpeg probe. The probe
> built and frame-verified BOTH a chained-`xfade` timeline and a self-contained
> overlay mid; they render pixel-identical pushes. **The overlay mid is the chosen
> architecture** — it drops into `_compose_floating`'s exact skeleton with pre/tail
> verbatim; xfade is demoted to a documented alternative (it needs a CFR-normalized
> popup and mode-dependent pre/tail boundaries).

## Goal and user-visible acceptance

The popup becomes a **third presentation mode** of the *same two recordings*
(main.webm + popup.webm), selected by config — no new recording behavior:

- `cut` — today's hard cut (unchanged).
- `float` — Spec B's floating window over the dimmed main (unchanged).
- `slide` (new) — the popup **slides in as a full window** (a **push-left** on open:
  the main content translates left and exits while the popup enters from the right)
  over `slide_ms`, holds **full-frame** while active, then **pushes right** (reverse)
  on close.

Acceptance:
1. In `slide` mode the popup interval is bracketed by two pushes (push-left in at
   `opened_at`, push-right out at `closed_at`) and is full-frame in between — never a
   hard cut, never a floating inset.
2. When the popup is open at end-of-main, the push-out is skipped (hold the popup
   full-frame to the last frame — mirrors Spec B's `hold_open_at_end`).
3. The popup renders **bare** (no address bar) exactly as in Spec B — `slide` reuses
   the bare-popup seam (see Render-side; this is recording behavior, not just
   compositor).
4. `cut` and `float` — and the `floating` bool — are byte-for-byte unchanged.

Out of scope (later cuts / future): N windows (>1 popup), popup→shell (address bar on
the popup + opener semantics), a scenario language to address/switch named windows.
This cut is purely the **slide transition** for the existing single-popup switch.

## Architecture — the stage (post-process, ffmpeg)

Same seam as Spec B: `video/mux.py::compose_popup_video` keeps **pre**
(`main[0:opened]`) and **tail** (`main[closed:]`) *verbatim* and replaces only the
middle, exactly like `_compose_floating`. `slide` adds a `_compose_slide` that reuses
the CFR-normalize → 3-way split → self-contained-mid → concat skeleton; the sliding
main frames come from the **mid split** (`main[opened..opened+D_in]` and
`main[closed−D_out..closed]`), so pre/tail seams are untouched (probe-verified
pixel-identical to the xfade formulation).

**Mid = two overlays over a CFR color base** (probe-validated, full-length,
seamless):

```
base = color=black:size=WxH:rate=FPS:duration=span            # pins output timing (VFR-safe)
prog = min( min(1, t/D_in), max(0, min(1, (span−t)/D_out)) )  # 0→1 in, 1, 1→0 out
[base][mid_main] overlay=x='-W*prog':y=0:eof_action=repeat    # main slides out left
[.. ][pop]       overlay=x='W*(1-prog)':y=0:eof_action=repeat # popup slides in from right
```

- `mid_main = main[opened:closed]` (from the 3-way split), `pop = [popup_cut]` (the
  existing trimmed/tpad'd popup, full-size — **reused verbatim**, no scaling).
- The two layers tile exactly (`main` covers `[−W·prog, W−W·prog)`, popup covers
  `[W−W·prog, …)`), same expression/rounding → no black seam (probe-confirmed).
- `hold_open_at_end`: drop the `(span−t)/D_out` term from `prog` (constant 1 after
  ramp-in) and skip the push-out; hold full-frame popup to the last frame.
- Mid ends `settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p`; concat
  `pre? + mid + tail?` (mid always consumed; the `has_pre × has_tail` matrix and the
  no-`null[outv]`-bypass rule are identical to `_compose_floating`).
- **Shares float's invariants:** CFR-normalize main before the split (backgrounded
  main may be frameless); the CFR color base pins length + `eof_action=repeat` holds
  the last real frame if an input is a frame short (NOT `pass`, which flashes black)
  repeat the popup's last frame (VFR-safe); and the **post-encode duration fail-loud
  guard** (`mux.py`: raise if produced < main_duration − tol) transfers unchanged.

Formulas: `D_in = min(slide_ms/1000, span/2)`, `D_out = min(slide_ms/1000, span −
D_in)`; `x_main = −W·prog`, `x_pop = W·(1−prog)`.

**Degenerate cases (well-defined, not skipped):** with the mid always consumed,
`opened_at≈0` (no pre) still pushes in — `main[0..D_in]` slides out even though no
pre is concat'd; `has_tail=False` (not `hold_open_at_end`) still pushes out revealing
`main[closed−D..closed]`, then the film ends. **Only `hold_open_at_end` skips the
push-out.**

**Guard `slide_ms=0`** (a valid "no slide" config): mirror float's `open_ms=0` guard —
when `D_in≤0` or `D_out≤0`, that phase's term is constant `1` (no `t/0`).

xfade alternative (documented, not used): a `slideleft`/`slideright` chain over three
trims works and is full-length, but requires the popup CFR-normalized and makes
pre/tail boundaries mode-dependent (`pre` to `opened+D_in`, `tail` from `closed−D_out`)
and the graph shape branch-dependent — worse fit than the overlay mid.

## Config (`PopupConfig`) — a transition mode with back-compat

Spec B shipped `floating: bool = True`. Generalize without breaking it:

- Add `transition: Literal["cut", "float", "slide"] | None = None` and `slide_ms: int
  = 400`. Both render-only, **outside `config_hash`** (all of PopupConfig already is;
  and `transition` changing *recording* behavior — bare vs chrome popup — is not a
  hash concern because `floating` already sets that precedent today).
- One derived accessor keeps the mapping in the model (never in mux):
  `effective_transition = transition or ("float" if floating else "cut")`. An explicit
  `transition` always wins (including `floating=False, transition="float"`).
- Bare-popup derivation lives here too, as a property:
  `is_bare = effective_transition in ("float", "slide")`. `render.py` reads
  `cfg.popup.is_bare` instead of `cfg.popup.floating` (see Render-side).
- `floating` stays a documented deprecated alias; existing scenarios unchanged.

`compose_popup_video` takes a new `transition: Literal["cut","float","slide"]` kwarg
(replacing the `floating: bool` at the call site); the render call passes
`cfg.popup.effective_transition`. The `floating → transition` mapping stays entirely
in the config model.

## Render-side

`slide` is bare exactly like `float`. The only changes:
- `render.py:598` `bare_popups = cfg.popup.floating` → `cfg.popup.is_bare` (so `slide`
  suppresses the legacy bar and the three fail-loud `expect_chrome` paths behave, per
  Spec B). Without this a `slide` popup would record **with** the chrome bar and
  acceptance #3 fails.
- Thread `transition`/`slide_ms` into the `compose_popup_video` call site and select
  the branch by `effective_transition` instead of the `floating` bool.

## Compositor cleanup (do as part of this)

`compose_popup_video` currently builds the identical `popup_filter` string twice
(once before the float branch, once on the non-floating path). A third mode
multiplies this — hoist the single `[popup_cut]` build and share it across all three
branches (`cut`/`float`/`slide`), matching the spec's "same trimmed/tpad'd popup".

## Testing

- `compose_popup_video(transition="slide")` on two synthetic clips: output duration ==
  main_duration; frame count == `round(fps × main_duration)` with monotonic PTS; pre
  and tail present. During the push-in window BOTH main and popup pixels are visible
  in one frame (a moving boundary — proves a push, not a cut); mid-interval is
  full-frame popup (centre AND border are popup, unlike `float`); push-out present
  unless `hold_open_at_end`.
- **Tail clock alignment:** a frame sampled just after `closed_at` equals main's frame
  at the same timestamp (catches offset/time-warp bugs).
- Clamp: interval < `2×slide_ms` renders without overrun; `slide_ms=0` renders (guard).
- `opened_at≈0` (no pre) renders with a push-in.
- Back-compat: `transition="cut"`/`"float"` reproduce Spec A/B exactly; `floating=True`/
  `False` still map to `float`/`cut`; `is_bare` matches.
- End-to-end (chromium+ffmpeg): a `slide` popup scenario renders a valid MP4 with the
  bare-popup seam intact.

## Decisions (defaults, confirmable)

- **Full-frame slide** (the window "takes over" — matches "switch to the window"), not
  inset/framed.
- **Direction:** push-left on open (new window enters from the right, main exits left);
  push-right on close.

## Definition of done

A scenario with `popup: {transition: slide}` renders an `.mp4` where the popup pushes
in full-frame, holds, and pushes out (or holds to the end), the popup has no address
bar, and `cut`/`float`/`floating` are unchanged. Full suite green.
