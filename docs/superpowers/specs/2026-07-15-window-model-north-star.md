# Window model — north star

Date: 2026-07-15
Status: agreed direction (not itself an implementation spec)
Scope: the shared architecture that Spec A (iframe shell + address bar), Spec B
(floating popup window), and Spec C (multi-window transitions) all build on.

This document exists so the first implementable spec (A) is designed to fit the
later ones instead of painting itself into a corner. It is intentionally short:
each lettered spec carries its own detailed, normative design.

## The problem this reframes

The three original requests for the browser "chrome" bar — cursor drives the
address bar, natural typing, and (most important) the page must render *below* the
bar and never be obscured — turned out not to be about a bar at all. They are the
first slice of a **window model**: the recorder should look like a real browser
handling one or more windows, with visible chrome, deliberate pointer interaction,
and legible transitions between windows.

Two follow-on desires confirmed the reframe: popups should look like real floating
windows (rounded frame, dimmed backdrop, emerge from the click), and opening/
switching windows should be a legible animation (e.g. a push left-to-right), not a
hard full-frame cut.

## The two layers

Everything below decomposes into exactly two layers with a clean seam.

### Layer 1 — per-window shell (lives *inside* the page recording)

Each browser window is a separate Playwright `Page` with its own video recording,
all sharing **one `BrowserContext`** so session state (cookies, `localStorage`,
logged-in identity) carries across windows. A window opened from another window
already "knows" what happened before it.

Each page *can* wear a **shell**: the browser chrome bar at the top and the
synthetic cursor, both as DOM overlays in the page's own top document, with the
target site loaded into an `<iframe>` that fills the area below the bar. Because the
site is inside the iframe, it can never paint into the bar's pixels — the "page
below the bar" guarantee (#3) is structural, not a heuristic. The main window gets
its shell in Spec A; **popups get their shell in Spec B** (converting a
`window.open` popup — whose top document is the site — into a shell page is a real
problem, see Open problems), and until then popups keep the legacy in-DOM bar.

The cursor, ripple, and highlight stay in the shell's DOM so they share one
coordinate space with the bar and with page content. Requirements #1 (cursor
drives the bar) and #2 (natural typing) are satisfied inside Layer 1, reusing the
existing cursor system unchanged.

Rationale for keeping chrome + cursor in DOM rather than in the compositor: they
must interact with live content and with each other in one coordinate space, and
the existing recording captures them "for free."

### Layer 2 — the stage (a post-process compositor over N recordings)

Above the recordings sits a **stage**: a compositor timeline built with ffmpeg
from the already-existing per-window recordings. It owns, for each moment: which
window(s) are visible, each window's transform (fullscreen vs. scaled/floating),
and the transition that connects one arrangement to the next.

The stage is where the floating popup (rounded frame, drop shadow, dimmed backdrop,
fade/scale-in from the click point) and multi-window choreography (push L→R on
open, window switching) live. It composes from separate recordings — there is no
live multi-page compositing.

Rationale for putting transitions in Layer 2: two per-window shells recorded
independently cannot slide over one another live, so any cross-window motion must
be composited in post regardless. One compositor then serves both the floating
popup and multi-window transitions — one mechanism, not three.

## How the recordings compose (the fact that unblocked this design)

Playwright records one video per `Page`, for that page's whole life, into
`record_video_dir`. A popup/new-window recording therefore captures the window's
*entire* lifetime — open, its own navigations, every interaction inside it, close —
not a "fresh clean browser." Windows share the context, so the second window's
recording reflects prior session state.

**Alignment is heuristic, not exact — and Spec B must reckon with that.** Today's
`compose_popup_video` (`video/mux.py`) uses two approximations, not one: it trims
the full wall-clock prime delay (`visual_ready_delay`, which by its own comment may
discard a few already-good real-site frames), *and* it tolerates an encoder-startup
gap up to `max(2.0s, 15%)` by **cloning** a frame via `tpad`. The monotonic
`anchor` itself is set ~100 ms+ after the main recording truly starts
(`render.py`), and the module calls the sync "approximate (K2)". This is fine for a
hard cut. But Spec B's "fade/scale-in from the click point" depends on the popup's
*first real frames* and on sub-frame `opened_at` accuracy — exactly the frames
currently trimmed or cloned. Spec B must either tolerate this (start the animation
from the first verified real frame, not the click instant) or add a tighter sync
before doing click-anchored animation.

## Sequencing

- **Spec A — iframe shell + address bar.** Establishes Layer 1 for the main
  window and delivers requirements #1/#2/#3. Foundation. (`compile` unchanged.)
- **Spec B — floating popup window.** Introduces Layer 2 (the stage) for a single
  popup: rounded frame, drop shadow, dimmed backdrop, fade/scale-in from the click,
  fade-out on close. Replaces the current hard cut. Also owns **popup → shell
  conversion** (Layer 1 for the popup, the Open-problems item) and, once that lands,
  removes the legacy in-DOM bar that Spec A leaves gated to popups.
- **Spec C — multi-window transitions.** Generalizes the stage to N windows with
  open/switch/close transitions (e.g. push L→R) and whatever scenario-language
  surface is needed to address windows.

Each lettered spec is its own `spec → plan → implement` cycle. This document is the
invariant they must not violate: **chrome + cursor in Layer 1 (DOM), all
cross-window arrangement and motion in Layer 2 (post-process stage), one shared
`BrowserContext` for session continuity.**

## Open problems (named, so a later spec owns them)

- **Popup → shell conversion (Spec B).** A popup is created by the site's own
  `window.open`, so its *top document is the site*, not our shell. Giving it a shell
  means the controller substitutes or wraps that page — which collides with things
  the current code and site rely on: the render invariant `popup.opener() is page`
  (`render.py`), and any site code holding the `window.open` WindowProxy
  (`postMessage`, `popup.close()`, opener writebacks). Session continuity across
  windows is *not* the hard part (same `BrowserContext` gives it for free, since
  popups are context-created today); the hard part is preserving opener semantics
  while inserting a shell. Spec B owns this; only after it lands is the legacy
  in-DOM bar removed.

## Known boundaries (deliberately deferred)

- **Interleaving** the main window and a popup *within one opening* (main → popup →
  main → popup) is blocked by **Layer 1 orchestration, not the compositor**:
  `_active_page` (`render.py`) routes every step to the open popup, and the "v1 one
  popup" invariant fails a second popup. Post-composition could concat N segments
  fine. Spec C, if it takes this on, fixes the orchestration, not `mux.py`. (Note: a
  backgrounded page may emit no frames under VFR, so any dimmed-backdrop/interleave
  plan relies on "decode the last frame" behavior — currently implicit.)
- Sites that inspect `window.top` or otherwise require being top-level may behave
  differently inside the iframe even with frame-busting neutralized (see Spec A
  risks).
