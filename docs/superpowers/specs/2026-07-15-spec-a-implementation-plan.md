# Spec A — implementation plan (parallelized)

Date: 2026-07-15
Spec: `2026-07-15-iframe-shell-address-bar-design.md`
Approach: 3 file-disjoint workstreams built in parallel (TDD), then one sequential
integration core that consumes them. Known pre-existing failures unrelated to this
work: `tests/unit/recorder/test_compile.py::test_popup_opened_during_reasoning_is_unexpected_and_click_is_not_run`
and `::test_popup_opened_during_click_preparation_is_not_attributed` (naive
substring redaction of the value "cli" inside the word "click"). Do not treat these
two as regressions.

## Round 1 — parallel, independent (disjoint files)

### WS-A — Config + config_hash  (`guidebot_recorder/models/config.py`)
New `ChromeConfig` fields (snake_case + camelCase alias, defaults shown):
- `interact_on_navigate`/`interactOnNavigate`: bool = True
- `char_delay_ms`/`charDelayMs`: int = 110
- `char_jitter_ms`/`charJitterMs`: int = 55
- `segment_pause_ms`/`segmentPauseMs`: int = 180
- `pre_navigate_pause_ms`/`preNavigatePauseMs`: int = 400
- `focus_color`/`focusColor`: str = "#3b82f6"
- `show_caret`/`showCaret`: bool = True

`config_hash` gains **only** `chrome.enabled` and `chrome.height` (bump
`CONFIG_HASH_VERSION`). Cosmetic chrome fields and all new fields above stay out.
Tests: alias parsing, defaults, `extra="forbid"` still holds; hash changes when
`enabled`/`height` change; hash unchanged when a cosmetic/typing field changes.
Files: `models/config.py`, `tests/unit/models/test_config*.py`.

### WS-B — Framing/header helper  (`guidebot_recorder/chrome/framing.py`, new)
Pure, browser-free helper (the unit-testable core):
```
def strip_framing_headers(headers: dict[str, str], *, is_document: bool) -> dict[str, str]
```
Removes `x-frame-options`; removes the `frame-ancestors` directive from
`content-security-policy` (keeping other directives); no-op when `is_document` is
False or headers absent; case-insensitive header names.

Plus the async installer (thin, integration-tested later):
```
async def install_framing(context, *, shell_origin: str) -> None
```
`context.route("**/*", handler)`: for document/subframe requests do
`resp = await route.fetch(max_redirects=0)`; if status in 300..399 →
`route.fulfill(response=resp)` unchanged (browser performs redirect, frame.url stays
truthful); else `route.fulfill(response=resp, headers=strip_framing_headers(...))`.
Non-document requests → `route.continue_()`.
Tests: `strip_framing_headers` unit matrix (XFO removed; frame-ancestors stripped
but script-src kept; passthrough when absent; case-insensitive). The async installer
gets a light test with a fake route/response double if feasible; otherwise leave a
`@pytest.mark.integration` stub.
Files: `chrome/framing.py`, `tests/unit/chrome/test_framing.py`.

### WS-C — Natural typing schedule  (`guidebot_recorder/chrome/typing.py`, new)
Pure function, deterministic:
```
def typing_schedule(text: str, *, char_delay_ms: int, char_jitter_ms: int,
                    segment_pause_ms: int, seed: str,
                    thinking_pause_ms: int = 500, thinking_rate: float = 0.06
                    ) -> list[int]
```
Returns one pre-character delay (ms) per character in `text` (len == len(text)).
Uses `random.Random(seed)` only — same seed ⇒ identical list. Boundary chars
`set("/.?#=&")` add `segment_pause_ms` **after** contributing. Jitter is uniform in
`[-char_jitter_ms, +char_jitter_ms]`, clamped so a delay is never negative. With
probability `thinking_rate` (seeded), add a one-off `thinking_pause_ms`.
Tests: determinism (same seed → equal list; different seed → different), length,
non-negative, boundary adds pause, jitter bounded.
Files: `chrome/typing.py`, `tests/unit/chrome/test_typing.py`.

## Round 2 — sequential integration core (consumes Round 1)

### WS-D-js — shell asset + role gating
- New `chrome/shell.js` + shell HTML: renders bar (dots + URL pill + lock + caret),
  hosts the site `<iframe>` (`top:HEIGHT`, `height:H-HEIGHT`, `sandbox` without
  `allow-top-navigation`), mounts the cursor. Exposes
  `window.__guidebot_shell` API: `pillRect()`, `focusPill()`, `blurPill()`,
  `clearUrl()`, `appendChar(ch)`, `setUrl(url)`.
- Role gating in `chrome/chrome.js` + `overlay/cursor.js`: capture
  `isTop = (window === window.top)` before any `top` shadowing; `SHELL_ORIGIN`
  check; roles shell | framed-site | popup-site (see spec). Legacy bar/cursor mount
  only in popup-site.
- Frame-bust neutralization (framed-site role only) after `isTop` captured.

### WS-D-py — pipeline integration
- `chrome/chrome.py`: install shell; `set_url` sources site `frame.url`; drive the
  choreography (overlay move → ripple → `focusPill` → `clearUrl` → per-char
  `appendChar` paced by `typing_schedule` → `blurPill`).
- `recorder/render.py`: context with `bypass_csp=True`, `service_workers="block"`,
  `install_framing(...)`; shell install for main page; navigation via
  `frame.goto` on the site frame; element actions/`reuse_is_valid` against the site
  frame; URL source `frame.url` in `_ensure_visuals`; navigate-while-popup keeps the
  legacy `page.goto` + `chrome.set_url` path.
- `recorder/recorder.py`: main-window actions target the site `Frame`;
  `locator.bounding_box()` used as-is (no `HEIGHT` offset).
- `recorder/compile.py`: compile the main site at `W×(H-HEIGHT)` when
  `chrome.enabled`.
- Tests: coordinate correctness (no offset), no-double-overlay, header/redirect,
  choreography ordering, config matrix, site-below-bar (integration, chromium).

## Definition of done
Full unit suite green (minus the two known pre-existing failures), integration
tests for the site-below-bar guarantee and header stripping passing where chromium/
ffmpeg is available, PR opened, PR reviewed.
