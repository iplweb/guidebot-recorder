# Spec C — window slide transitions (the "stage", v2)

Date: 2026-07-19
Status: draft (for fable self-review + ffmpeg probe, then plan)
Builds on: `2026-07-15-window-model-north-star.md` (Layer 2 — the stage), Spec A
(iframe shell) and Spec B (floating popup — the compositor stage).

First cut of the north-star's multi-window layer: an animated **slide/push**
transition when the film switches to and from a window, instead of the hard cut
(original) or the floating overlay (Spec B). Matches the original ask: "opening a
new window and switching to it is a push-left-to-right animation."

## Goal and user-visible acceptance

The popup becomes a **third presentation mode** of the *same two recordings*
(main.webm + popup.webm), selected by config — no new recording behavior:

- `cut` — today's hard cut (unchanged).
- `float` — Spec B's floating window over the dimmed main (unchanged).
- `slide` (new) — the popup **slides in as a full window** (push left-to-right:
  the popup enters from the right while the main exits to the left) over `slide_ms`,
  holds **full-frame** while active, then **slides out** (reverse: popup exits
  right, main re-enters) on close.

Acceptance:
1. In `slide` mode the popup interval is bracketed by two push transitions (in at
   `opened_at`, out at `closed_at`) and is full-frame in between — never a hard cut,
   never a floating inset.
2. When the popup is open at end-of-main, the slide-out is skipped (hold the popup
   full-frame to the last frame — mirrors Spec B's `hold_open_at_end`).
3. The popup renders **bare** (no address bar) exactly as in Spec B (`slide` reuses
   the bare-popup seam; only the compositor treatment differs).
4. `cut` and `float` are byte-for-byte unchanged.

Out of scope (later cuts of Spec C / future): N windows (>1 popup), popup→shell
(address bar on the popup + opener semantics), a scenario language to address/switch
named windows. This cut is purely the **slide transition** for the existing
single-popup switch.

## Architecture — the stage (post-process, ffmpeg)

Same seam as Spec B: `video/mux.py::compose_popup_video` keeps pre (`main[0:opened]`)
and tail (`main[closed:]`) and replaces the middle. For `slide`:

- The popup body during the interval is the **full-frame** popup cut (the same
  trimmed/tpad'd `[popup_cut]` used by `cut`, at full size — no scaling).
- **Slide-in** (first `slide_in_eff` of the interval): an `xfade`-style push where
  main exits left and popup enters right. ffmpeg `xfade=transition=slideleft:
  duration=D:offset=O` composites exactly this between two equal-size streams; the
  transition runs at the pre→middle boundary.
- **Slide-out** (last `slide_out_eff`): the reverse push (`slideright`) at the
  middle→tail boundary (skipped when `hold_open_at_end`).
- Clamp like Spec B: `slide_in_eff = min(slide_ms/1000, span/2)`,
  `slide_out_eff = min(slide_ms/1000, span − slide_in_eff)`.

Because `xfade` needs both streams present across the transition window, the slide-in
consumes the tail of `pre` (main sliding out) together with the head of the popup;
the slide-out consumes the tail of the popup together with the head of `tail` (main
sliding back in). The exact filtergraph (whether to express the whole timeline as a
chain of `xfade`s or to build the middle as a self-contained slide segment and
concat) is a **feasibility item for the probe** — `xfade` across concat seams needs
careful `offset`/PTS handling.

CFR/equal-size invariants carry over from Spec B (both recordings at
`record_video_size`; main may be frameless while backgrounded → CFR-normalize before
slicing, same as `_compose_floating`).

## Config (`PopupConfig`) — introduce a transition mode

Spec B shipped `floating: bool = True`. Generalize to a mode without breaking it:

- Add `transition: Literal["cut", "float", "slide"]` (render-only, outside
  `config_hash`, like the rest of PopupConfig).
- Back-compat: when `transition` is unset, derive it from `floating`
  (`True → "float"`, `False → "cut"`). Setting `transition` explicitly wins. Keep
  `floating` as a documented deprecated alias so existing scenarios are unchanged.
- New: `slide_ms: int = 400` (per-direction slide duration).

(Default stays `float` via the `floating=True` default — `slide` is opt-in.)

## Render-side

No new render behavior: `slide` reuses Spec B's bare-popup seam. The one wiring
change is threading `transition`/`slide_ms` (alongside the existing PopupConfig
params) into the `compose_popup_video` call site, and selecting the compositor branch
by `transition` instead of the `floating` bool.

## Risks and mitigations

- **xfade across concat seams (primary risk).** `xfade` merges two streams over a
  window; stitching pre + slide-in + hold + slide-out + tail with correct offsets
  and monotonic PTS is fiddly. Mitigation: the fable probe builds and runs the real
  filtergraph on two test clips; fallback is a self-contained middle segment that
  does the slide via time-parameterized `overlay=x=f(t)` of two full-frame layers
  (main and popup) rather than `xfade`, then concat as today.
- **Direction semantics.** "Push L→R" = new window enters from the right, main exits
  left (like advancing forward); close reverses. Confirm the reading matches the
  user's mental model (decision below).
- Interval shorter than `2×slide_ms`: clamped (as Spec B), so the two slides meet
  without overrun.

## Decisions to confirm (surfaced, with defaults)

- **Full-frame slide vs framed slide.** Default: the slid-in window is **full-frame**
  (it "takes over" the screen — matches "switch to the window"). Not inset/framed.
- **Direction.** Default: open = push left (new window in from right); close = push
  right (main back from left).

## Testing

- `compose_popup_video(transition="slide")` on two synthetic clips: output duration
  == main_duration; pre and tail present; during the slide-in window BOTH main and
  popup pixels are visible in the same frame (a moving boundary — proves a push, not
  a cut); mid-interval is full-frame popup (centre AND border are popup, unlike
  `float`); slide-out present unless `hold_open_at_end`.
- Clamp: interval < 2×slide_ms renders without overrun.
- Back-compat: `transition="cut"` and `transition="float"` reproduce Spec B/A exactly;
  `floating=True`/`False` still map to `float`/`cut`.
- End-to-end (chromium+ffmpeg): a `slide` popup scenario renders a valid MP4 with the
  bare-popup seam intact.

## Definition of done

A scenario with `popup: {transition: slide}` renders an `.mp4` where the popup slides
in full-frame (push L→R), holds, and slides out (or holds to the end), the popup has
no address bar, and `cut`/`float`/`floating` are unchanged. Full suite green.
