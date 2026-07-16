# Spec B — floating popup window (the "stage", v1)

Date: 2026-07-16
Status: approved direction (decisions taken), ready for self-review then plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 2 — the stage) and Spec A.
Introduces the post-process **stage** compositor for a single popup and replaces
the current hard-cut popup compositing (`video/mux.py::compose_popup_video`).

## Goal and user-visible acceptance

Make a popup look like a real floating window emerging from the click, instead of
a full-frame hard cut.

1. When the recorded click opens the popup, the popup **scales + fades in from the
   click point** to a centered, rounded-corner window with a drop shadow, over the
   **dimmed** main page (the main page never leaves the screen).
2. While the popup is active, it stays as that floating window; the main recording
   remains visible (dimmed) behind it.
3. On popup close, the window **fades/scales out** and the main page returns to full
   brightness.
4. The popup renders **bare** — no address bar, no legacy chrome — so the only
   window chrome is the compositor-drawn frame (no double chrome).

Out of scope for Spec B (→ Spec C): multiple popups/windows, window switching,
slide/push transitions, popup→shell conversion, an address bar on the popup.

## Decisions taken

- **Bare popup + compositor frame.** The popup does not get the iframe shell or the
  legacy in-DOM bar. This sidesteps the north-star open problem (converting a
  `window.open` popup into a shell while preserving `opener` semantics): we never
  touch the popup's document, we only *frame its recording* in post.
- **Scale + fade-in from the click point, dimmed backdrop, fade-out on close.**

## Architecture — the stage (post-process, ffmpeg)

Both recordings already exist as separate files on one timeline (main.webm,
popup.webm) with `opened_at`/`closed_at` offsets on the main clock (unchanged from
today). Instead of concatenating (hard cut), the stage **overlays**:

For the interval `[opened_at, closed_at]`, each output frame =
`dim(main_frame) ⊕ framed(scaled(popup_frame))`, where:

- `dim(main)`: the main recording darkened (e.g. `eq=brightness=-B`) — optionally a
  light `boxblur` — so it reads as an inactive backdrop.
- `scaled(popup)`: the popup recording scaled to `POPUP_SCALE` of the frame
  (default ~0.72), centered.
- `framed(...)`: a rounded-corner mask + drop shadow drawn around the scaled popup
  (a pre-rendered RGBA "window frame" PNG sized to the scaled popup, `overlay`-ed;
  rounded corners via an alpha mask so the popup's own square corners are clipped).
- **Open transition** (first `T_in` ≈ 320 ms of the interval): animate the popup
  layer's position from the click point to center and its scale `0→POPUP_SCALE`
  with alpha `0→1`. Position/alpha are time-parameterized in the `overlay`/`scale`
  expressions (functions of `t`); the click point `(cx, cy)` comes from render (see
  below).
- **Close transition** (last `T_out` ≈ 240 ms): reverse (scale/alpha down, back
  toward the click point), then main returns to full brightness.

Outside `[opened_at, closed_at]` the output is the plain main recording (full
brightness), exactly as today.

Frame sizes: every page in the context records at `record_video_size`
(`cfg.viewport`), so main and popup are the same size — the scale factor is applied
in the compositor, not by Playwright (unchanged invariant).

### Timing vs. the heuristic alignment (north-star finding #1)

`opened_at` is approximate and the popup's earliest frames are trimmed
(`visual_ready_delay`) or cloned (`tpad`). The scale-in therefore anchors its
**timing** to the popup's first *verified* real frame (`opened_at +
visual_ready_delay`), not the raw click instant, so it never animates a blank/prime
frame. The click **point** `(cx, cy)` is only a *spatial* origin for the motion, so
its sub-frame timing imprecision is invisible. This keeps the effect robust against
the existing approximate sync.

## Render-side changes (`recorder/render.py`)

- **Bare popup.** When a popup opens, do not install the shell and suppress the
  legacy chrome bar for that page (extend the existing role gating: a popup in a
  floating-window render mounts neither bar). The cursor overlay MAY still be shown
  inside the popup (it participates in popup interactions) — TBD-free default: keep
  the cursor, drop the bar.
- **Capture the click point.** The opening click's `before_click`/`mark_click_started`
  callback records *timing only*, not coordinates. The scale-in origin `(cx, cy)`
  is therefore taken from the cursor's last viewport position — `Overlay.pos` after
  `_point_and_prepare` moved it to the target center — captured at the opening click
  and threaded to the compositor. (Only needed if the scale-from-point polish is
  enabled; fade-in needs no coordinates.)
- Everything else in the one-popup lifecycle (detection window, `opened_at`/
  `closed_at`, quiescence, fail-loud on a second/unexpected popup) is unchanged.

## Compositor changes (`video/mux.py`)

- Replace `compose_popup_video`'s hard-cut concat with the overlay stage above,
  keeping its signature plus new params: `click_point=(cx, cy)`, and the frame /
  dim / scale / transition settings. Preserve the existing trim math
  (`visual_ready_delay`, startup-gap clamp) for choosing which popup frames are
  real; feed the post-trim popup stream into the scaled overlay.
- The pre/tail main segments (before `opened_at`, after `closed_at`) stay as plain
  main video; only the popup interval becomes an overlay composite.
- One H.264 encode of the final stream (unchanged).

## Config (`PopupConfig`, new, render-only, outside `config_hash`)

All cosmetic → outside `config_hash` (no recompile). Defaults chosen to look right
untuned:

- `floating: bool = True` (off → today's hard cut, for back-compat / A-B).
- `scale: float = 0.72`, `corner_radius: int = 14`, `shadow: bool = True`.
- `backdrop_dim: float = 0.45` (0 = none), `backdrop_blur: int = 0`.
- `open_ms: int = 320`, `close_ms: int = 240`.

## Feasibility (probed with ffmpeg — 2026-07-16)

The **core composite is proven**: `eq=brightness=-0.30` dims the main; the popup is
`scale`d to `0.72` (922×518 at 1280×720), given rounded corners via a `format=rgba`
+ `geq` alpha mask (corner radius `r`, alpha 0 outside the rounded rect), faded in
with `fade=t=in:alpha=1:d=0.32`, and `overlay`-ed centered over the dimmed main —
produced a valid stream. So the **primary** rendering is: dim backdrop + rounded
scaled popup + drop shadow (a pre-blurred black rounded rect `overlay`-ed just
behind the popup) + **fade-in/out**. High confidence.

The **scale-from-the-click-point growth is optional polish**, not load-bearing:
`scale` is not time-animatable, so the growth needs `zoompan` (or a per-frame
scaled layer). If that proves brittle at integration time, ship fade-only — the
dimmed-backdrop framed window already delivers the effect. The spec therefore
treats scale-from-point as an enhancement gated behind a clean `zoompan` probe.

## Risks and mitigations

- **Animated scale (polish only).** See Feasibility: primary is fade-in (proven);
  scale-from-point is a gated enhancement with a proven fade-only fallback.
- **VFR / backgrounded frames** (north-star): the dimmed main behind the popup
  relies on the main recording still having frames during the popup interval; if
  the main page is backgrounded and emits none, hold its last frame. Verify.
- **Cursor inside the popup** at reduced scale may look small; acceptable for v1,
  revisit if unreadable.

## Testing

- `compose_popup_video` (floating): with two synthetic recordings, assert the
  output duration equals main's, the popup interval frames are a composite (popup
  pixels appear scaled/inset, main pixels visible at the dimmed border), and
  pre/tail are untouched. (ffmpeg-marked.)
- Geometry: the scaled popup is centered at `POPUP_SCALE` with the frame margin.
- Transition bounds: at `opened_at` the popup layer alpha≈0 near the click point;
  by `opened_at+open_ms` it is centered at full alpha.
- Back-compat: `floating=False` reproduces today's hard cut (existing popup
  integration test still passes).
- Bare popup: a floating-window popup render mounts no chrome bar in the popup.

## Definition of done

A scenario whose click opens a popup renders an `.mp4` where the popup emerges as a
dimmed-backdrop floating window from the click point and closes back, the popup
carries no address bar, the main page is continuously visible behind it, and
`floating=False` still yields the old hard cut. Full suite green.
