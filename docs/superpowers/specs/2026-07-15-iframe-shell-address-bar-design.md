# Spec A — iframe shell + address bar interaction

Date: 2026-07-15
Status: approved after self-review, ready to write implementation plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 1, main window).
Supersedes, for the main window during render, the current in-DOM chrome bar that
reserves space via `padding-top` on `<html>` (`guidebot_recorder/chrome/chrome.js`).

## Goal and user-visible acceptance

Render the main window as a real-looking browser: a chrome bar on top, the target
site strictly below it, and a pointer-driven, naturally typed address bar on
navigation.

1. **#3 (most important) — page never obscured.** The target site loads inside an
   `<iframe>` that occupies only the viewport region below the bar. The site cannot
   paint into the bar's pixels regardless of its own CSS (fixed/sticky top headers,
   `100vh`, full-bleed heroes). This is structural, not a padding heuristic.
2. **#1 — pointer drives the bar.** On a `navigate` step the synthetic cursor
   glides to the URL pill, performs a click (ripple), the pill takes a focused look
   (focus ring + caret), the old URL is cleared, and only then does typing begin.
3. **#2 — natural typing.** The new URL is typed character by character with a
   base per-character delay plus jitter, longer pauses at segment boundaries
   (`/`, `.`, `?`), and an occasional brief "thinking" pause, then a short settle
   before the iframe navigates. Timing is deterministic across re-renders.
4. **Any site frames.** `X-Frame-Options` and CSP `frame-ancestors` are stripped
   from responses and common JS frame-busting is neutralized so arbitrary sites
   load in the iframe.

Out of scope for Spec A: floating popup window, dimmed backdrop, any cross-window
transition, multi-window support (all Spec B/C). `compile` is unchanged.

## Architecture

### Shell document

Render's top-level document becomes a **shell** we own, not the target site:

- A fixed chrome bar at the top, height `HEIGHT` (existing `ChromeConfig.height`),
  carrying the existing macOS-style dots + URL pill (+ lock).
- An `<iframe>` filling `top: HEIGHT` down to the viewport bottom, width `100%`,
  `border: 0`, into which the target site loads.
- The synthetic cursor overlay (existing `overlay/cursor.js`) mounted in the shell,
  above both the bar and the iframe.

The shell is a small static HTML/JS asset served/inserted by the Python controller
(evolution of `guidebot_recorder/chrome/`). The iframe's geometry is the single
source of the `(0, HEIGHT)` offset used to map in-page coordinates to shell
viewport coordinates.

### Header stripping and frame-busting neutralization

- Context created with `bypass_csp=True`.
- A `page.route` (or context route) handler removes `x-frame-options` and any
  `frame-ancestors` directive from `content-security-policy` on document/subframe
  responses before they reach the renderer.
- An init script neutralizes the common frame-busting idioms (`if (top !== self)
  top.location = self.location`, `top.location.href = ...`) by making the
  iframe's `window.top` / `window.parent` comparisons benign for the framed
  document. Kept deliberately light: this covers the common cases, not every
  anti-framing trick.

This rewrites a site's framing-protection headers. That is acceptable for a
recording/demo tool driving chosen sites; it is called out as an explicit,
intended behavior, not a silent side effect.

### Render pipeline changes (localized; compile untouched)

`compile` continues to load the target site **top-level** and resolve
selectors/identity there. The DOM inside the render-time iframe is the same site
DOM, so compiled targets and identity checks remain valid; only the Playwright
handle they run against changes from `Page` to the site `Frame`.

In `guidebot_recorder/recorder/render.py` and the `Recorder`:

- **Navigation** sets the iframe's `src` (and awaits its load) instead of
  `page.goto`. Redirects are reflected by reading the frame's final URL after load.
- **Element actions** (`click`, `hover`, `type`, `waitFor`, `reuse_is_valid`)
  target the site `Frame` rather than the top `Page`.
- **Cursor positioning** maps a frame-relative bounding box to shell viewport
  coordinates by adding `(0, HEIGHT)`. The cursor/ripple/highlight APIs are
  unchanged; only the coordinates handed to them shift.
- **Navigation containment.** In-iframe navigations and `target=_blank` attempts
  that would replace or escape the top document are bound to the iframe (or, when
  they legitimately open a new window, handled by the existing popup path — see
  "Popups during Spec A").
- `_ensure_visuals` continues to guarantee the bar + cursor survive SPA DOM
  rewrites; it now also re-asserts the shell/iframe invariant.

### Address bar choreography (navigate step)

Replaces the current `Chrome.set_url` text-only animation. On a `navigate` step,
when chrome is enabled, `show_url` is on, and `interact_on_navigate` is on:

1. Glide the cursor to the URL pill center (shell coordinates) at the existing
   distance-scaled duration; settle.
2. Ripple (click) on the pill; the pill switches to a **focused** look (focus ring
   in `focus_color`, blinking caret if `show_caret`).
3. Clear the old URL (select-all-then-replace feel — the old text vanishes).
4. Type the new URL character by character per the timing model below.
5. Brief `pre_navigate_pause_ms` settle, drop the focused look, then set the
   iframe `src` and await load.

When `interact_on_navigate` is off, fall back to the current instant/typed pill
update with no cursor/click choreography (back-compat).

### Natural typing model (#2)

Per character, the inter-key delay is:

- base `char_delay_ms`
- plus jitter in `[-char_jitter_ms, +char_jitter_ms]`
- plus `segment_pause_ms` when the just-typed character is a boundary (`/`, `.`,
  `?`, `#`, `=`, `&`)
- plus an occasional small "thinking" pause at a low, bounded rate.

**Determinism (decision):** the jitter and thinking-pause draws come from a seeded
PRNG seeded from the target URL plus the step index — the same scenario re-renders
to the byte-identical timeline. The rest of the pipeline is deterministic; typing
must not break that. (Rejected alternative: unseeded randomness per render.)

Defaults aim for a calm, readable pace (slower than the current fixed 24 ms),
since a separate agent handles audio and the URL should be legible as it appears.

## Config additions (`ChromeConfig`)

All new fields have defaults and are render-only (outside `config_hash`), so no
recompile is triggered. Existing scenarios keep working unchanged.

- `interact_on_navigate: bool = True` — run the cursor→click→type choreography.
  `type_on_navigate` remains as a back-compat alias.
- Typing timing: `char_delay_ms`, `char_jitter_ms`, `segment_pause_ms`,
  `pre_navigate_pause_ms` (sensible calm defaults).
- Focus look: `focus_color`, `show_caret: bool`.

Field count is kept small on purpose; the built-in defaults should look right with
no `chrome:` tuning.

## Popups during Spec A

Popups are **left exactly as they are today** (their own recording, the current
in-DOM `padding-top` chrome bar, hard-cut compositing). The iframe shell applies to
the **main window only** in Spec A.

Reason: `window.open(url)` navigates the *popup's own top document* to the site, so
wrapping a popup in a shell (site-in-iframe) belongs to the stage work in Spec B/C,
not here. The old chrome mechanism therefore stays in the tree until Spec B lands,
at which point it is removed. This is a brief, contained two-mechanism overlap with
no regression to existing popup behavior. (Rejected alternative: render popups with
no chrome bar during Spec A — a visible regression.)

## Risks and mitigations

- **Sites that require being top-level.** A site inspecting `window.top`, expecting
  to be the top document, or using anti-framing beyond the neutralized idioms may
  behave differently in the iframe. Mitigation: light frame-bust neutralization
  covers common cases; genuinely hostile sites are out of the tool's remit and
  should fail loudly rather than render wrong.
- **Header rewriting.** Stripping `X-Frame-Options`/`frame-ancestors` is a
  deliberate, documented behavior of the tool, scoped to render.
- **Coordinate drift.** The `(0, HEIGHT)` offset must be applied consistently
  everywhere the cursor targets in-page geometry; a single missed site would put
  clicks/ripples in the wrong place. Covered by tests asserting the offset on a
  known target.
- **Navigation escaping the iframe.** In-iframe links/`target=_blank` that try to
  replace the top document must be contained to the iframe or routed through the
  popup path; an uncontained one would blow away the shell.

## Testing

- Site-below-bar guarantee against adversarial CSS: a page with a `position:fixed;
  top:0` header and a `100vh` hero renders entirely below `HEIGHT` (assert no site
  pixels above the bar).
- Header stripping: a response carrying `X-Frame-Options: DENY` and
  `frame-ancestors 'none'` loads in the iframe.
- Coordinate offset: a known in-iframe target maps to shell coordinates shifted by
  exactly `(0, HEIGHT)`; the cursor lands on it.
- Typing determinism: two renders of the same navigate step produce identical
  per-character timing.
- Choreography ordering: on `navigate`, cursor-move → ripple → focus → clear →
  type → navigate occur in that order.
- Back-compat: `interact_on_navigate: false` reproduces the current pill-only
  behavior; existing scenarios render without config changes.

## Definition of done

Chrome enabled + a `navigate` step yields an `.mp4` where the site sits entirely
below the bar (including adversarial CSS), the cursor visibly moves to the bar,
clicks, and types the URL naturally, and arbitrary framing-protected sites load.
`compile` output and existing non-chrome renders are unchanged.
