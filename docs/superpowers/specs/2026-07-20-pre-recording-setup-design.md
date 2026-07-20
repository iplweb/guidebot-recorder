# Pre-recording setup — cached session with a login health-check

**Date:** 2026-07-20
**Status:** approved, ready to plan

## Problem

You often need the site in a prepared state before the *interesting* part of a
video is recorded: logged in, cookie banner already accepted, tenant selected.
Today the only way to reach that state is to author those actions as ordinary
scenario steps — and because recording is armed at browser-context creation
(`recorder/render.py:1705`, `record_video_dir=...`), everything a page does from
`context.new_page()` onward is captured. **The login and the cookie click end up
on the film.**

There is no un-recorded "prepare the environment first" phase. A repo-wide grep
confirms the codebase has zero `storage_state`, `add_cookies`,
`launch_persistent_context`, or `user_data_dir`: every `render`, `compile`, and
localized variant gets a fresh, empty context, and the docs tell authors to
arrange login/cookie state themselves as filmed steps
(`docs/pl/scenario-reference.md:71`, `docs/pl/how-it-works.md:62`).

## Goal

Let a recording reuse a **prepared, cached browser session** (Playwright
`storage_state`) established by *another scenario* — typically an existing
scenario that already teaches logging in. The preparation runs on a context that
is **not** recording, so it can never appear in the video. A cheap health-check
decides whether the cached session is still valid; if not, it is refreshed
automatically.

## Non-goals (YAGNI)

- No new dedicated `setup.yaml` file type. The setup source is an ordinary
  `*.scenario.yaml` that is already compiled.
- No inline per-scenario setup block. The setup source is always a referenced,
  shared scenario file.
- No interactive "record your login" codegen. The manual case (MFA/captcha) is
  served by running the refresh `--headed` so a human can finish by hand.
- No natural-language / LLM health-check. Liveness is a deterministic text
  match. No Codex, no compiled target, for `verify`.
- v1 keeps **one** session cache per setup scenario, language-agnostic — reused
  across localized variants (auth is orthogonal to `locale`).
- Setup replays only frozen targets. Render still makes **zero** LLM calls.

## Terminology

- **Setup scenario** — an ordinary `*.scenario.yaml` (with its
  `*.compiled.yaml` sidecar) whose steps leave the browser in the desired
  prepared state. Reusing an existing "how to log in" scenario is the intended
  path.
- **Target scenario** — the scenario being recorded, which references a setup
  scenario via `config.setup`.
- **Session cache** — a Playwright `storage_state` JSON persisted under
  `.guidebot/sessions/`, reused across renders.

## Schema changes (`models/config.py`)

Two optional additions to `Config`, both ignored during a normal render and only
consulted when a scenario acts as a setup source or references one.

### On the target scenario — reference the setup

```yaml
config:
  setup: teach-login.scenario.yaml   # path, relative to this scenario
```

`setup: Optional[str]`. When present, render runs the pre-recording session
phase (below) before creating the recording context.

### On the setup scenario — the health-check

```yaml
config:
  baseUrl: https://example.com
  verifyUserLoggedIn: "Wyloguj"      # shorthand: this text must appear on baseUrl
  # full form (all fields optional except containsText):
  # verifyUserLoggedIn:
  #   containsText: "Wyloguj"
  #   url: /dashboard                # default: baseUrl
  #   timeout: 8                     # default: 8s
  maxAgeHours: 12                    # optional TTL for proactive refresh
```

- `verifyUserLoggedIn` accepts a **string** (equivalent to `{containsText: ...}`)
  or an object `VerifyLoggedIn { containsText: str, url: Optional[str],
  timeout: float = 8 }`.
- Match semantics: `containsText` is a plain substring of the target page's
  visible text (`body.innerText`), **case-sensitive**, compared after `trim`.
- `url` defaults to `baseUrl`. It is a rarely-needed override; the normal case is
  "load `baseUrl`; a logged-in root shows the text, a logged-out root redirects
  to a login form that does not."
- If `verifyUserLoggedIn` is absent, the health-check is skipped: a present cache
  is reused, and refresh happens only via `maxAgeHours`, `--force`, or a missing
  cache.
- `maxAgeHours: Optional[float]`. When set, a cache older than this is treated as
  stale regardless of the text check.

Both fields are cosmetic-to-render (they never affect a normal recording), so
they stay outside the compile hash and do not force recompilation.

## Session cache

- **Location:** `.guidebot/sessions/<key>.json`, alongside the existing
  `.guidebot/audio/` cache.
- **Format:** exactly the object returned by Playwright
  `context.storage_state()` (cookies + `localStorage` origins).
- **Key:** a stable hash of `(resolved setup scenario path, setup baseUrl, setup
  compiled-target hash)`. Changing the setup scenario's login flow (hence its
  compiled targets) changes the key and abandons the old session.
- **Git:** add `.guidebot/sessions/` to `.gitignore`. The file contains live
  auth tokens and must never be committed.

## Execution flow

### New CLI command

```
guidebot setup SETUP_SCENARIO [--headed] [--force]
```

Builds or refreshes the session cache for a setup scenario:

1. Require the setup scenario's `*.compiled.yaml` (error, instructing
   `guidebot compile`, if missing/stale — setup replays frozen targets only).
2. Open an un-recorded context (`browser.new_context()` with **no**
   `record_video_dir`), `overlay=None`.
3. Replay the setup scenario's compiled action steps, skipping render-only
   entries (`say`, `slide`, cursor/chrome/intro/sound). `${ENV}` substitution
   works as today; env-derived values are redacted in logs via the existing
   `scenario_env_references` machinery.
4. `storage_state = await context.storage_state()`; write it to the cache.
5. Run the health-check (below). On success, report and exit. On failure with
   `--headed`, pause for manual completion, then re-snapshot and re-check.
6. `--force` ignores any existing cache and always rebuilds.

### Health-check (`verifyUserLoggedIn`)

Runs on a throwaway un-recorded context seeded with the cached `storage_state`:
`goto(url or baseUrl)` → read `body.innerText` → assert it contains
`containsText` within `timeout`. Returns a boolean "session live". No LLM, no
compiled target.

### Render pre-phase (`recorder/render.py`, before line 1705)

When the target scenario has `config.setup`:

```
resolve + load setup scenario (+ its compiled sidecar)
compute cache key
session_live = false
if cache exists and (maxAgeHours not exceeded):
    session_live = run health-check on cached storage_state
        (if verifyUserLoggedIn absent → treat present, unexpired cache as live)
if not session_live:
    # auto-refresh
    run setup replay (un-recorded) → snapshot → save cache
    session_live = run health-check
    if not session_live:
        FAIL LOUDLY: "session could not be established automatically;
        run `guidebot setup <setup> --headed` to finish login by hand"
# hand off to the recording context
```

Then the sole change at the recording boundary:

```python
context = await browser.new_context(
    viewport=...,
    locale=...,
    record_video_dir=str(work),
    record_video_size=...,
    storage_state=<cache path>,     # NEW — only when config.setup is set
    **({...} if cfg.chrome.enabled else {}),
)
```

Everything the load-verify-refresh logic does happens on separate, non-recording
contexts *before* this line. The prepared state therefore **cannot** reach the
video.

## Error handling

- Missing/stale cache, failing health-check, or exceeded `maxAgeHours` → silent
  auto-refresh (re-run setup replay).
- Auto-refresh that still fails the health-check → **stop the render** with a
  clear message pointing at `guidebot setup <setup> --headed`. Render never
  silently produces a video from a logged-out session.
- Setup scenario not compiled → stop with an instruction to compile it first.
- Auth values are redacted in all logs (reuse `scenario_sensitive_values`).

## Security

- `.guidebot/sessions/*.json` holds live cookies/tokens → gitignored, never
  logged.
- Credentials continue to flow only through `${ENV}` substitution
  (`DEMO_EMAIL`, `DEMO_PASSWORD`) into the setup scenario's `enterText`/
  `navigate` values.

## Testing (TDD)

Unit:

- Cache key derivation is stable and changes with the setup compiled-target
  hash.
- `storage_state` round-trips (save → load).
- Reuse-vs-refresh decision table: cache present + check passes → reuse; check
  fails → refresh; `maxAgeHours` exceeded → refresh; `verifyUserLoggedIn`
  absent + fresh cache → reuse; `--force` → refresh.
- `verifyUserLoggedIn` string shorthand parses to the object form.

Integration (local test server, mirroring the injected cookie-banner harness at
`render.py:1552`):

- Setup replay "logs in" (server sets a cookie); health-check text present →
  render receives the `storage_state`; **the recorded video does not contain the
  login page**.
- Falsifying test: cache seeded with an expired/invalid cookie → health-check
  text absent → auto-refresh is triggered and rebuilds the session.
- Auto-refresh that cannot succeed → render fails loudly with the `--headed`
  instruction (no video emitted).

## Files to touch

- `models/config.py` — `Config.setup: Optional[str]`; `verifyUserLoggedIn`
  (string | `VerifyLoggedIn`); `maxAgeHours: Optional[float]`.
- `recorder/session.py` (new) — cache keying, `storage_state` load/save, the
  health-check, the load-verify-refresh decision, setup replay driver.
- `recorder/render.py` — pre-phase before line 1705; thread `storage_state` into
  `new_context`.
- `cli.py` — `setup` command; `validate` accepts setup scenarios (already
  scenarios, so mostly free).
- `.gitignore` — `.guidebot/sessions/`.
- `examples/` — an example target scenario with `config.setup`, plus a
  `verifyUserLoggedIn` on the login example.
- `docs/` (EN + PL) + README — document the setup reference, the session cache,
  and `guidebot setup`.

## Open questions

None blocking. Deferred by choice: `containsText` as a list ("all must match"),
case-insensitive matching, and per-variant sessions — all easy later additions
if a real need appears.
