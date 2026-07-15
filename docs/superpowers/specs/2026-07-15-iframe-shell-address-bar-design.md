# Spec A — iframe shell + address bar interaction

Date: 2026-07-15
Status: approved after fable self-review (corrections applied), ready to write implementation plan
Builds on: `2026-07-15-window-model-north-star.md` (Layer 1, main window).
Supersedes, for the main window during render, the current in-DOM chrome bar that
reserves space via `padding-top` on `<html>` (`guidebot_recorder/chrome/chrome.js`).

> Revision note: this version integrates two fable design reviews. The most
> consequential change from the first draft is the **viewport geometry decision**
> (below): when chrome is enabled, the main window's site viewport genuinely
> shrinks, so chrome now participates in the main window's compile geometry and
> `config_hash` — reversing the first draft's "chrome is purely render-only"
> assumption. This is deliberate and is the one point worth an explicit veto.

## Goal and user-visible acceptance

Render the main window as a real-looking browser: a chrome bar on top, the target
site strictly below it, and a pointer-driven, naturally typed address bar on
navigation.

1. **#3 (most important) — page never obscured.** The target site loads inside an
   `<iframe>` that occupies only the viewport region below the bar. The site cannot
   paint into the bar's pixels regardless of its own CSS (fixed/sticky top headers,
   `100vh`, full-bleed heroes). Structural, not a padding heuristic.
2. **#1 — pointer drives the bar.** On a `navigate` step the synthetic cursor
   glides to the URL pill, clicks (ripple), the pill takes a focused look (focus
   ring + caret), the old URL is cleared, and only then does typing begin.
3. **#2 — natural typing.** The new URL is typed character by character with a base
   per-character delay plus jitter, longer pauses at segment boundaries
   (`/`, `.`, `?`), and an occasional brief "thinking" pause, then a short settle
   before the frame navigates. The per-character delay *sequence* is deterministic
   across re-renders.
4. **Any site frames.** `X-Frame-Options` and CSP `frame-ancestors` are stripped
   from document responses and common JS frame-busting is neutralized so arbitrary
   sites load in the iframe.

Out of scope for Spec A: floating popup window, dimmed backdrop, any cross-window
transition, multi-window support (all Spec B/C).

## Viewport geometry (decision — read first, it drives the rest)

The context viewport and `record_video_size` stay at `cfg.viewport` (`W × H`), so
the output MP4 keeps the configured dimensions and **popups are geometrically
untouched**. Within the main window's shell:

- shell top document: `W × H`
- chrome bar: top strip, height `HEIGHT` (`ChromeConfig.height`)
- site iframe: `left/right 0`, `top HEIGHT`, height `H − HEIGHT`; the site's layout
  viewport is therefore `W × (H − HEIGHT)`.

Unlike the old `padding-top` bar (which left the site's layout viewport at `H`, so
`vh`/media-queries were unchanged), an iframe genuinely shrinks the layout viewport.
If compile still ran at full `H` while render ran the framed site at `H − HEIGHT`,
a responsive breakpoint or `100vh`/`@media (max-height…)` rule could yield a
different DOM at render than at compile, breaking `reuse_is_valid`
(`resolver/validate.py`) with no fingerprint signal — and `compile --force` would
not help, because it would revalidate at full `H`.

Therefore, **when `chrome.enabled` is true, the main window compiles at
`W × (H − HEIGHT)`**, matching the render-time iframe interior. Consequences:

- `config_hash` (`models/config.py:126`) gains `chrome.enabled` and `chrome.height`
  (only these two; the cosmetic chrome fields stay out). Toggling chrome on/off or
  changing its height forces a recompile. This is honest: enabling chrome now
  changes the site's render geometry.
- Popups keep `cfg.viewport` at both compile and render (their old `padding-top`
  bar preserves the `H` layout viewport, so they stay consistent — see "Popups
  during Spec A").

Rejected alternative: grow the context viewport to `W × (H + HEIGHT)` to keep the
main compile untouched. It changes the output MP4 dimensions and, because the
context viewport is global, shifts popups' layout viewport too (breaking their
compile/render consistency). The chosen option keeps output dims standard and
popups clean, at the cost of coupling chrome into the main compile.

## Architecture

### Shell document and role detection

Render's main-window top document is a **shell** we own, not the target site. It is
loaded from a recognizable sentinel URL/origin (e.g. an internal
`https://guidebot.shell/` fulfilled by a route) so injected scripts can detect their
role deterministically even after frame-busting shadows `window.top`. The shell
contains:

- the fixed chrome bar (macOS dots + URL pill + lock), height `HEIGHT`;
- the site `<iframe>` (geometry above);
- the synthetic cursor overlay (`overlay/cursor.js`) mounted in the shell, above
  both bar and iframe.

Every injected script must first compute its **role** at the very top, before any
frame-bust shadowing runs:

```
const isTop = (window === window.top);          // captured BEFORE top is shadowed
const isShell = location.origin === SHELL_ORIGIN;
// role: shell | framed-site (isTop === false) | popup-site (isTop && !isShell)
```

- **shell** → mounts its own bar + cursor; the context-level old `chrome.js`/
  `cursor.js` must early-return here.
- **framed-site** (the iframe) → mounts neither bar nor cursor (the shell owns
  them); only the header/frame-bust neutralization applies.
- **popup-site** (a top-level popup document, no shell) → keeps today's old
  `chrome.js` padding bar + `cursor.js` (unchanged behavior).

This gating is required because `context.add_init_script` runs in **every** frame
(`render.py:513-517`, `Chrome.install_context`, `Overlay.install_context`);
without it the old `chrome.js` would paint a second bar and add `padding-top`
*inside* the iframe and `cursor.js` would mount a duplicate cursor there. Script
order matters: capture `isTop` before shadowing `top`, or the iframe misidentifies
as top.

### Header stripping, service workers, and frame-busting

- Context: `bypass_csp=True` and `service_workers="block"` (SW-served documents
  never hit `context.route`, so their `X-Frame-Options`/`frame-ancestors` could not
  otherwise be stripped — this is a behavior change for SW-heavy sites, called out).
- A `context.route` handler does `route.fetch(max_redirects=0)`; it **passes 3xx
  responses through unchanged** (so the browser performs the redirect and
  `frame.url` reflects the true final URL) and strips `x-frame-options` and the
  `frame-ancestors` directive from `content-security-policy` only on 2xx document
  responses before `route.fulfill`.
- An init script neutralizes common frame-busting (`if (top !== self) top.location
  = …`, `top.location.href = …`) by shadowing the framed document's `top`/`parent`
  (both are `[Replaceable]`, so this is feasible) — applied only in the
  **framed-site** role, and only after `isTop` is captured.

Rewriting a site's framing-protection headers and blocking its service worker are
deliberate, documented behaviors of the render step, scoped to render.

### Iframe containment (top navigation / `target=_blank`)

The site iframe uses `sandbox` **without** `allow-top-navigation*` (e.g.
`allow-scripts allow-same-origin allow-forms allow-popups allow-modals
allow-popups-to-escape-sandbox`) so a recorded click (user activation) cannot make
`<a target="_top">`/`window.top.location` blow away the shell. Side effects
(downloads, some OAuth top-redirect flows) are accepted for Spec A and noted as a
validation risk. As a backstop, an unexpected top-frame navigation on the shell
page is a fail-loud `RenderError`.

`target=_blank`/new windows: a click compiled with `opens_popup` continues through
the existing popup path (`render.py:783-841`); any *other* new page remains a
fail-loud `RenderError` exactly as today (`_unexpected_pages`,
`render.py:571-572,599-600`). The spec claims no new "escape" tolerance.

### Render pipeline changes (localized)

`compile` resolves selectors/identity against the site as before, only at the new
`W × (H − HEIGHT)` viewport when chrome is enabled (above). The DOM is the same site
DOM; the Playwright handle it runs against becomes the site `Frame`.

In `render.py` / `Recorder` for the **main window** (site frame):

- **Navigation** uses `frame.goto(url)` on the site frame (awaits commit+load;
  avoids the `wait_for_load_state`-returns-for-old-document race of a bare `src`
  set). The pill/URL source is `frame.url` after load, so redirects are reflected.
- **Element actions** (`click`/`hover`/`type`/`waitFor`/`reuse_is_valid`) target the
  site `Frame`.
- **Cursor positioning** uses `locator.bounding_box()` on the site-frame locator,
  which Playwright returns **relative to the main-frame viewport** — i.e. it already
  includes the iframe's offset. **No `(0, HEIGHT)` offset is applied.** An explicit
  offset is only correct for geometry read *inside* the frame via
  `frame.evaluate(getBoundingClientRect)`, which Spec A does not use.
- **URL reflection into the bar**: everywhere the old code passed `page.url` to
  `chrome.ensure(...)` (`render.py:185-190`, `chrome.py:55`), the shell path passes
  the site `frame.url`. Otherwise `_ensure_visuals` (which runs before/after every
  step, `render.py:579,602,747`) would flip the pill to the shell/`about:blank` URL.
- **`_ensure_visuals` invariant (reworded)**: under the shell it re-asserts *the
  iframe is present, the bar exists in the shell document, and the cursor exists in
  the shell document* — not "the site survived an SPA rewrite" (the site can no
  longer touch the shell DOM at all).

A `navigate` step while a popup is active is driven on the popup page with today's
`page.goto` + `chrome.set_url` pill path (popups have no shell), so "navigate = set
the site frame" is the main-window rule, not universal.

### Address bar choreography (main-window navigate step)

Replaces `Chrome.set_url`'s text-only animation. Gated on `chrome.enabled`,
`show_url`, and the config matrix below. Sequence:

1. Glide cursor to the URL pill center (shell coords from `bounding_box`); settle.
2. Ripple (click); pill switches to **focused** look (focus ring `focus_color`,
   caret if `show_caret`).
3. Clear the old URL (select-all-then-replace feel).
4. Type the new URL per the timing model.
5. `pre_navigate_pause_ms` settle, drop focused look, then `frame.goto(url)`.

### Natural typing model (#2)

Per-character inter-key delay = base `char_delay_ms` + jitter in
`[-char_jitter_ms, +char_jitter_ms]` + `segment_pause_ms` after a boundary char
(`/ . ? # = &`) + an occasional bounded "thinking" pause. Jitter and thinking-pause
draws come from a **seeded PRNG** (seed = target URL + step index), so the
*per-character delay sequence* is identical across re-renders. (Execution is via
`setTimeout` on a VFR screencast, so wall-clock/frames are not byte-identical — the
determinism guarantee is on the delay sequence, which is what the tests assert.)
Defaults aim for a calm, legible pace (slower than the current fixed 24 ms).

## Config: navigate-interaction matrix

Two independent flags plus the existing per-step override — no "alias":

- `type_on_navigate: bool = True` (existing): typed pill vs instant pill.
- `interact_on_navigate: bool = True` (new): run the cursor→click→focus
  choreography before typing.
- per-step `navigate` override (`step.navigate_type_override()`,
  `render.py:755-757`): continues to force `type_on_navigate` for that step.

Decision matrix (main window; chrome enabled + `show_url`):

| `interact` | `type` | behavior |
|---|---|---|
| true | true | full choreography: cursor→click→focus→natural type→navigate (new default) |
| false | true | typed pill only, no cursor choreography (today's animated behavior) |
| any | false | instant pill, no typing/choreography (today's instant behavior) |

Existing scenarios that set `typeOnNavigate: false` keep instant pills. Existing
scenarios with chrome + defaults are upgraded to full choreography (intended new
look; opt out with `interactOnNavigate: false`).

Other new `ChromeConfig` fields (render-only, defaults, outside `config_hash`):
typing timing (`char_delay_ms`, `char_jitter_ms`, `segment_pause_ms`,
`pre_navigate_pause_ms`), focus look (`focus_color`, `show_caret`). Only
`enabled` + `height` enter `config_hash`.

## Popups during Spec A

Popups are **left as today**: their own recording, the old in-DOM `padding-top`
chrome bar (now explicitly gated to the *popup-site* role), hard-cut compositing
(`mux.py`), `cfg.viewport` geometry at compile and render. The iframe shell applies
to the **main window only** in Spec A.

The old `chrome.js`/`cursor.js` therefore stay in the tree, gated by role. They are
removed only once **Spec B** converts popups to shell pages (that conversion —
including `window.open` → shell and `opener` semantics — is Spec B/C work, per the
north star's open-problems section, not Spec A). This is a contained, no-regression
overlap of the two mechanisms.

## Risks and mitigations

- **Sites requiring top-level context.** A site inspecting `window.top`, or using
  anti-framing beyond the neutralized idioms, may behave differently framed.
  Mitigation: light neutralization covers common cases; genuinely hostile sites are
  out of remit and should fail loudly.
- **`sandbox` side effects.** Blocking top navigation may break legitimate
  top-redirect flows (some OAuth) and downloads. Validate against target sites; the
  fail-loud backstop prevents silent wrong renders.
- **`service_workers="block"`** changes behavior for SW-heavy sites (PWAs, offline
  caches).
- **Header rewriting** is deliberate and render-scoped.
- **Compile coupling.** Chrome now affects the main compile viewport + `config_hash`;
  a stale sidecar after toggling chrome is caught by the fingerprint and re-run.

## Testing

- **Site-below-bar** against adversarial CSS: a page with `position:fixed;top:0`
  header and a `100vh` hero renders entirely below `HEIGHT` (no site pixels above
  the bar).
- **No double overlay**: the site iframe contains no second chrome bar and no
  second cursor (role gating), and `<html>` inside the iframe has no injected
  `padding-top`.
- **Header stripping + redirects**: a response with `X-Frame-Options: DENY` and
  `frame-ancestors 'none'` loads framed; a 301→200 chain leaves `frame.url` (and the
  pill) at the final URL, not the request URL.
- **Coordinate correctness**: a known in-iframe target's `bounding_box()` lands the
  cursor on it with **no** extra `HEIGHT` offset.
- **Typing determinism**: two renders of the same navigate step produce the identical
  per-character delay sequence.
- **Choreography ordering**: cursor-move → ripple → focus → clear → type → navigate.
- **Config matrix**: each row of the table produces its stated behavior; existing
  chrome scenarios render without config changes; `typeOnNavigate:false` stays instant.
- **`config_hash`**: toggling `chrome.enabled` or changing `chrome.height` changes
  the hash (forces recompile); changing a cosmetic chrome field does not.

## Definition of done

Chrome enabled + a main-window `navigate` step yields an `.mp4` at the configured
dimensions where the site sits entirely below the bar (including adversarial CSS),
the cursor visibly moves to the bar, clicks, and types the URL naturally with a
truthful final URL, and arbitrary framing-protected sites load. Popup renders are
unchanged. `config_hash` reflects chrome geometry.
