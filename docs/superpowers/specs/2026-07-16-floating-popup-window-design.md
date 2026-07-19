# Spec B — floating popup window (the "stage", v1)

Date: 2026-07-16
Status: approved after fable self-review (corrections applied), ready for plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 2 — the stage) and Spec A.
Introduces the post-process **stage** compositor for a single popup and replaces
the current hard-cut popup compositing (`video/mux.py::compose_popup_video`).

> Revision note: integrates a fable code-review. Two scope changes from the first
> draft: (1) **scale-from-the-click-point growth is deferred** to a follow-up — the
> proven, robust deliverable is fade-in over a dimmed backdrop (still a floating
> window emerging in place), so the `click_point` plumbing is cut from Spec B; (2)
> the "bare popup" seam is spelled out because it touches three fail-loud Python
> paths, not just role gating.

## Goal and user-visible acceptance

Make a popup look like a real floating window instead of a full-frame hard cut.

1. When the recorded click opens the popup, the popup **fades in** (from
   transparent to full) as a centered, rounded-corner window with a drop shadow,
   over the **dimmed** main page (the main page never leaves the screen).
2. While the popup is active, it stays as that floating window; the main recording
   remains visible (dimmed) behind it.
3. On popup close, the window **fades out** and the main page returns to full
   brightness — unless the popup is still open at the end of the recording, in
   which case the framed popup + dim are held to the last frame (no phantom close).
4. The popup renders **bare** — no address bar, no legacy chrome — so the only
   window chrome is the compositor-drawn frame (no double chrome).

Out of scope for Spec B: **scale-from-click-point growth** (follow-up polish — see
Deferred), multiple popups/windows, window switching, slide/push transitions,
popup→shell conversion, an address bar on the popup (all Spec C or later).

## Decisions taken

- **Bare popup + compositor frame.** The popup gets neither the iframe shell nor the
  legacy in-DOM bar; we never touch the popup's document, only *frame its recording*
  in post. Sidesteps the north-star `window.open`→shell / `opener` open problem.
- **Fade-in over a dimmed backdrop, fade-out on close.** (Scale-from-point deferred.)

## Architecture — the stage (post-process, ffmpeg)

main.webm and popup.webm already sit on one timeline with `opened_at`/`closed_at`
offsets (unchanged). The current code splits main into **pre** (`[0:opened_at]`) and
**tail** (`[closed_at:]`) and concats `pre + popup_cut + tail`. Floating mode keeps
pre and tail **verbatim** and replaces only the middle:

```
middle = overlay( dim(main[opened_at:closed_at]),  framed_scaled_faded(popup_cut) )
out    = concat( pre?, middle, tail? )
```

- `dim(main_mid)`: `eq=brightness=-backdrop_dim` (+ optional `boxblur`). The dim is
  ramped over `open_eff` (see transitions) so the backdrop darkens as the popup
  fades in rather than snapping; symmetric un-ramp over `close_eff`.
- `framed_scaled_faded(popup_cut)`: popup scaled to `scale` (centered), rounded
  corners via `format=rgba`+`geq` alpha mask, a pre-blurred rounded black rectangle
  `overlay`-ed just behind it as the drop shadow, and `fade=t=in:alpha=1` /
  `fade=t=out:alpha=1` at the interval ends.
- Every segment (incl. the overlay output) ends `setsar=1,format=yuv420p` under the
  same `settb=AVTB,setpts=PTS-STARTPTS` discipline as pre/tail so `concat` joins
  cleanly. One final H.264 encode (unchanged).

### The backdrop must be CFR (critical — main is backgrounded during the popup)

Once the popup opens, render brings the popup to front (`_active_page` →
`bring_to_front`), so the **main page is backgrounded for the whole interval** and
Playwright's VFR screencast can emit **zero frames** between `opened_at` and
`closed_at`. A raw `trim=start=opened_at:end=closed_at` on `[0:v]` would then yield
an empty/short backdrop → the overlay/concat fails or the film comes out shorter
than `main_duration`, tripping the audio-bed duration guards.

Fix: normalize the main stream to **CFR** (`fps=FPS`) before splitting, so gaps
clone the last real frame across the whole interval; additionally `tpad` the middle
backdrop to exactly `popup_span` and **fail loud** if it is still empty. Pin the
overlay length to the backdrop (`overlay` default repeats the popup's last frame if
shorter).

### The split is 3-way, and the degenerate cases matter

The middle **always** consumes main, so the split count is `1 + has_pre + has_tail`
(`split=3` / `split=2` / no split). `main_sources` needs a `"mid"` entry in every
branch. The current `len(labels)==1 → null[outv]` shortcut is **wrong** for floating
(it would bypass the overlay); when there is no pre and no tail (popup opens at ~0
and stays open to the end), `[0:v]` must still route into `dim → overlay`. Duration/
PTS math is otherwise symmetric to pre/tail. The existing trim/tpad/startup-gap math
on the popup (choosing which popup frames are real) transfers **unchanged** — only
the *consumer* of `[popup_cut]` changes from concat to the scaled overlay.

### Transition timing

- Anchor `t=0` of the middle to the first **verified** popup frame: the code already
  shifts `opened_at` by `visual_ready_delay`, so the mid segment's `t=0` is that
  frame; tpad's cloned startup frames land under the fade-in (benign).
- Clamp to the interval: `open_eff = min(open_ms, span/2)`,
  `close_eff = min(close_ms, span − open_eff)`. `fade=t=out` starts at
  `span − close_eff` on the PTS-reset middle.
- **No phantom close.** When the popup is open at end-of-main (`popup_open_at_end`
  / no tail), skip the close fade/un-dim and hold the framed popup + dim to the last
  frame. Thread this flag into the compositor.

## Render-side changes (`recorder/render.py`) — the bare-popup seam

The popup must render with **no chrome bar**, and three fail-loud paths currently
assume the bar exists on every non-shell page. The seam:

- **Suppress the mount.** Add `barePopups: true` to the `window.__guidebot_chrome_config`
  prelude (`chrome.py`) when floating; the popup-site branch of `chrome.js` returns
  before mounting (safe: the only top-level non-shell page in a render context is the
  popup — the site is framed, the shell is the sentinel origin).
- **Don't demand the bar on popup pages** (`expect_chrome=False`, threaded from
  `observe_page` — any non-first page is the popup): 
  - `_prime_visuals`'s status script must not require `[data-guidebot-chrome]` for
    the popup, or the prime loop never stabilizes and raises "nie udało się
    zainicjować warstw wizualnych" — **killing every floating render**.
  - `_ensure_visuals`'s non-shell branch must not `chrome.ensure`/assert the chrome
    API for the popup.
  - `_prepare_popup` must not call `chrome.ensure(page)` for the bare popup.
- The cursor overlay **is** kept on the popup (it drives popup interactions); only
  the bar is suppressed.
- The one-popup lifecycle (`_wait_for_render_popup`, `_sync_popup_close`, unexpected/
  second-popup fail-loud, `opened_at`/`closed_at`) is **untouched**.
- **No `click_point` plumbing** (deferred with the scale polish).

## Compositor changes (`video/mux.py`)

- `compose_popup_video` gains a `floating: bool` (+ the PopupConfig cosmetics) and
  branches: `floating=False` keeps **today's filtergraph verbatim** (early branch);
  `floating=True` uses the CFR-normalized 3-way split + dimmed overlay above. The
  shared validation/trim math (everything before the concat assembly) is identical
  and reused for both.
- New params: `scale`, `corner_radius`, `shadow`, `backdrop_dim`, `backdrop_blur`,
  `open_ms`, `close_ms`, `hold_open_at_end`.

## Config (`PopupConfig`, new, render-only)

Nests as `Config.popup: PopupConfig = Field(default_factory=PopupConfig)`
(`extra="forbid"`, so the YAML `popup:` key is added). All fields are compositor
cosmetics → **outside `config_hash`** (matches the `CursorConfig` precedent; no
`CONFIG_HASH_VERSION` bump). Note: `floating=True` *does* change the popup's DOM (the
legacy bar padding is gone, content shifts up), but it stays hash-safe because
compiled popup targets are **locator-based** and re-validated by `reuse_is_valid`
at render, not pixel-frozen.

Fields (defaults chosen to look right untuned): `floating: bool = True`,
`scale: float = 0.72`, `corner_radius: int = 14`, `shadow: bool = True`,
`backdrop_dim: float = 0.45`, `backdrop_blur: int = 0`, `open_ms: int = 320`,
`close_ms: int = 240`.

**Default flips existing output.** `floating=True` by default changes every existing
popup scenario's rendered output on upgrade with no config change — intentional (the
new look is the point). Opt out with `popup: {floating: false}`, which reproduces
today's exact hard cut.

## Cosmetic decisions (v1)

- **Two cursors during the interval.** The dimmed backdrop retains the frozen main
  cursor at the click point while the active cursor lives in the scaled popup.
  Accepted for v1 (reads as "you clicked here, this opened").
- **Dim ramp,** not a hard step (ramped over `open_eff`), so the backdrop darkens
  with the fade-in.
- Cursor inside the scaled popup is smaller; accepted for v1.

## Deferred (follow-up polish)

Scale-from-the-click-point growth: animate the popup from `(cx,cy)` (from
`Overlay.pos`, snapshotted inside `mark_click_started`, frame-center fallback if the
target box is `None`) growing to centered, via `zoompan` — behind its own ffmpeg
probe (the review flagged `scale` as not time-animatable). Ships only if the probe
is clean; the dimmed-backdrop fade-in already delivers the floating-window effect.

## Testing

- `compose_popup_video(floating=True)` with two synthetic recordings: output
  duration equals `main_duration`; the popup interval is a composite (scaled popup
  pixels inset, dimmed main visible at the border); pre/tail untouched. (ffmpeg.)
- **Empty-backdrop guard:** a main recording with no frames in the interval still
  yields a full-length dimmed backdrop (CFR clone), not an error.
- 3-way split degenerate cases: popup opens at ~0 with no pre; popup open to
  end-of-main with no tail (and no phantom close transition).
- Transition clamp: a popup interval shorter than `open_ms+close_ms` still renders
  (fades clamped, no overlap/overrun).
- Bare popup: a floating render mounts **no** chrome bar on the popup and
  `_prime_visuals` stabilizes without one.
- Back-compat: `floating=False` reproduces today's hard cut — the existing popup
  integration test sets `floating=False` (a required test change).

## Definition of done

A scenario whose click opens a popup renders an `.mp4` where the popup fades in as a
dimmed-backdrop rounded floating window and fades out (or holds to the end), the
popup carries no address bar, the main page is continuously visible behind it,
`floating=False` yields the old hard cut, and the full suite is green.
