# Pre-recording setup — cached session with a login health-check

**Date:** 2026-07-20
**Status:** approved, ready to plan (revised after Fable 5 design review)

## Problem

You often need the site in a prepared state before the *interesting* part of a
video is recorded: logged in, cookie banner already accepted, tenant selected.
Today the only way to reach that state is to author those actions as ordinary
scenario steps — and because recording is armed at browser-context creation
(`recorder/render.py:1705`, `record_video_dir=...`), everything a page does from
`context.new_page()` onward is captured. **The login and the cookie click end up
on the film.**

There is no un-recorded "prepare the environment first" phase. A repo-wide grep
confirms zero `storage_state`, `add_cookies`, `launch_persistent_context`, or
`user_data_dir`: every `render`, `compile`, and localized variant gets a fresh,
empty context, and the docs tell authors to arrange login/cookie state
themselves as filmed steps.

## Goal

Let a recording reuse a **prepared, cached browser session** (Playwright
`storage_state`) established by running ANOTHER already-compiled scenario —
typically an existing scenario that already teaches logging in — on a context
that is **not** recording. A cheap, deterministic text health-check
(`verifyUserLoggedIn`) decides whether the cached session is still valid; if not,
it is refreshed automatically.

## Terminology

- **Setup scenario** — an ordinary `*.scenario.yaml` (+ its `*.compiled.yaml`
  sidecar) whose steps leave the browser in the desired prepared state. Reusing
  an existing "how to log in" scenario is the intended path.
- **Target scenario** — the scenario being recorded, which references a setup
  scenario via `config.setup`.
- **Session cache** — a Playwright `storage_state` persisted (wrapped) under
  `.guidebot/sessions/`, reused across compiles and renders.

## The load-bearing insight (from review §1)

Removing the login steps from the *target* scenario means the target must be
**compiled AND rendered while already logged in**. If compile runs against a
fresh, empty context it lands on the login page and `resolve_step_target` either
fails or freezes garbage against the login DOM. Therefore the session pre-phase
must run **before both `compile` and `render`** of the target — not render only.
This is the central correction to the first draft.

## Establishing the session — reuse the compile machinery (review §4)

Rather than writing a third copy of the step loop (after `run_compile` and
`run_render`), session establishment **replays the setup scenario through the
existing compile path** with a reasoner that always raises:

- `RaisingReasoner` — any call to resolve/infer raises `SetupNeedsCompile`. So a
  setup sidecar whose targets are all reuse-valid replays cleanly (drives the
  page from frozen targets); anything that would need an LLM fails loudly and
  tells the user to run `guidebot compile <setup>` first. Render/setup still make
  **zero** LLM calls.
- Running through the compile path inherits, for free: the pop-up lifecycle
  (SSO/OAuth login pop-ups are a canonical login flow — `compile.py:321-416`),
  optional/gate **pending** entries (probe-and-skip when the element is absent),
  readiness waits, `_resolve_url` against the setup's `base_url`, `${ENV}`
  substitution, and secret redaction.
- The replay context uses the **setup scenario's `site_viewport` and `locale`**
  (matching how its targets were frozen, `compile.py:213-217`), never
  `record_video_dir`.

Implementation seam: factor the inner context/replay of `run_compile_in_browser`
so a caller can (a) provide/own the context and (b) capture
`context.storage_state()` **before the context closes**. The session module owns
that context, runs the replay, snapshots, then closes.

`opens_popup` and `pending` are thus supported (not just tolerated). If a future
setup flow hits a genuinely unsupported shape, it fails loudly via the raising
reasoner or the existing pop-up contract — never silently.

## Schema changes (`models/config.py`, `extra="forbid"`)

### On the target scenario — reference the setup

```yaml
config:
  setup: teach-login.scenario.yaml   # path, relative to this scenario
```

`setup: Optional[str]`. **Not cosmetic** (review §1): its presence changes the
DOM that compile resolves against, so it participates in target-compile
invalidation (see "Compile invalidation").

A scenario used as a setup source **must not itself declare `config.setup`**
(recursion guard, review §8) — validation error.

### On the setup scenario — the health-check

```yaml
config:
  baseUrl: https://example.com
  verifyUserLoggedIn: "Wyloguj"      # shorthand for {containsText: "Wyloguj"}
  # full form (all fields optional except containsText):
  # verifyUserLoggedIn:
  #   containsText: "Wyloguj"
  #   url: /dashboard                # default: target baseUrl (see health-check)
  #   timeout: 8                     # default: 8s
  maxAgeHours: 12                    # optional TTL (see cache)
```

- `verifyUserLoggedIn` accepts a **string** (= `{containsText: ...}`) or an
  object `VerifyLoggedIn { containsText: str, url: Optional[str],
  timeout: float = 8 }`.
- Match: plain substring of the page's rendered `document.body.innerText`,
  **case-sensitive** (no whole-page "trim" — that clause is dropped). Docs must
  tell authors to choose text that renders **only when authenticated** (a
  username is the robust choice); substring matching has no word boundaries, so a
  logged-out footer "wyloguj się kiedy chcesz" would false-positive.
- Both `verifyUserLoggedIn` and `maxAgeHours` are render-only on the setup file
  (stay outside the setup file's own compile hash).
- **If a setup source declares neither `verifyUserLoggedIn` nor `maxAgeHours`**
  (review §7): allowed, but emit a **loud warning** at setup/compile/render time.
  The "never silently logged-out" guarantee below holds **only when a
  health-check is configured**; without one a present cache is trusted until
  `--force`.

## Session cache

- **Location:** `.guidebot/sessions/<key>.json`, beside `.guidebot/audio/`.
- **Format (wrapper, review §9):**
  ```json
  { "created_at": "<ISO-8601 UTC>", "key_inputs": {...}, "storage_state": {...} }
  ```
  `maxAgeHours` is computed from `created_at`, never file mtime (survives
  `git clean`, copies, CI restore). The inner `storage_state` dict is passed
  directly to `new_context(storage_state=<dict>)` — Playwright accepts a dict, so
  no raw-file round-trip.
- **Key (review §2):** `sha256` over canonical JSON of:
  `{v, setup_path (Path.resolve()), setup_baseUrl, setup_config_hash,
  env_digest}` where `env_digest = sha256(sorted (name,value) pairs of
  scenario_env_references(setup_path, env))`. Credentials are folded into the
  combined hash only — never raw, never a standalone filename component (avoids
  offline guessing of a low-entropy password). Changing the login user changes
  the key → old session abandoned. Key is pre-wired so `locale` can be added
  later without breaking (review §6, per-variant sessions deferral).
- **Writes:** atomic (`tmp` + `os.replace`); file `0o600`, dir `0o700`
  (review §10). Concurrent render-set variants share it safely.
- **Gitignore that travels (review §10):** on first use, write
  `.guidebot/sessions/.gitignore` containing `*` — the cache protects itself in
  the *user's* repo, not only this one. (This repo's root `.gitignore` also gets
  `.guidebot/sessions/`.)

## Health-check (`verifyUserLoggedIn`) — review §3

Runs on a throwaway un-recorded context seeded with the cached `storage_state`:

1. `goto(verify.url or TARGET baseUrl)`. The check uses the **target** scenario's
   `baseUrl` when available (cookies are origin-scoped; a green check on the
   setup origin means nothing if the render loads a different host).
2. **Host guard:** if the target's `baseUrl` host differs from the setup's,
   hard-error in v1 (cross-origin session reuse is not sound). Documented.
3. Poll, don't snapshot: `wait_for_function` on
   `document.body.innerText.includes(<containsText>)` up to `timeout`. This
   avoids false negatives on async SPA shells.
4. Returns a boolean "session live". No LLM, no compiled target. Error output
   **never** includes `body.innerText` (PII/usernames) — only pass/fail and the
   configured `containsText` is echoed.

Known, documented false modes: an app that paints logged-in chrome then JS-
redirects to `/login` can transiently satisfy the check; authors pick
authenticated-only text to mitigate. Cross-origin and word-boundary limits are
listed in docs.

## Execution flow

### Shared pre-phase: `ensure_session(browser, setup_scenario, env, ...) -> dict`

Used by **both** target-compile and target-render:

```
require setup scenario compiled (else error: run `guidebot compile <setup>`)
compute cache key
live = false
if cache exists and (maxAgeHours not exceeded):
    live = health-check(cached storage_state)          # skipped→live if no verify configured
if not live:
    # auto-refresh: replay setup via compile-path + RaisingReasoner (un-recorded)
    storage_state = replay_setup(...)                  # popups/pending/redaction inherited
    save cache (atomic, 0600)
    live = health-check(storage_state)
    if not live:
        FAIL LOUDLY. Message distinguishes:
          - health-text-not-found  → check verifyUserLoggedIn / try `guidebot setup <setup> --headed`
          - session-not-persisted  → "this app may keep its session outside
            cookies/localStorage (sessionStorage/IndexedDB) — pre-recording setup
            cannot cache it" (review §5)
return storage_state (dict)
```

### Target render (`recorder/render.py`, before line 1705)

When target has `config.setup`: call `ensure_session(...)`, then the single
boundary change:

```python
context = await browser.new_context(
    viewport=..., locale=..., record_video_dir=str(work),
    record_video_size=...,
    storage_state=<session dict>,        # NEW — only when config.setup is set
    **({"bypass_csp": True, "service_workers": "block"} if cfg.chrome.enabled else {}),
)
```

All load-verify-refresh happens on separate, non-recording contexts *before* this
line. The prepared state **cannot** reach the video.

### Target compile (`recorder/compile.py`, before line 214) — review §1

When target has `config.setup`: call the same `ensure_session(...)`, then seed
the compile context:

```python
context = await browser.new_context(
    viewport={"width": site_width, "height": site_height},
    locale=cfg.locale,
    storage_state=<session dict>,        # NEW — only when config.setup is set
)
```

So the Reasoner resolves targets against the logged-in DOM.

### Compile invalidation (review §1)

Adding/removing/changing `config.setup` must invalidate the target sidecar.
The `config_hash` projection (`models/config.py`) is extended with the `setup`
path (only when set), so adding/removing/repointing `setup` re-resolves.
`verifyUserLoggedIn`/`maxAgeHours` stay out of the hash.

**Implemented scope (post code-review):** only the `setup` *path* enters the
target hash, not the setup `env_digest`. `config_hash(cfg)` is a pure function of
`Config` and cannot see `env`, and threading credentials into it would touch
every fingerprint-stamping and currency-check call site. Consequence: switching
the login credential to a *different user whose authenticated DOM differs
structurally* refreshes the session (the cache key **does** include
`env_digest`) but does not by itself recompile the target. This is an accepted
v1 limitation, bounded because (a) same-user credential rotations leave the DOM
shape unchanged, and (b) render performs a live identity check for
click/hover/type, so a genuine DOM drift **fails loudly** (prompting
`guidebot compile --force`) rather than producing a wrong video silently.
Folding `env_digest` into the target compile fingerprint is deferred.

### CLI: `guidebot setup SETUP_SCENARIO [--headed] [--force]`

- Plain (live healthy cache, review §8): **check-and-exit**, report "session
  reused". Does not replay.
- Missing/stale/failed check: replay + save.
- `--force`: always rebuild, ignoring any cache.
- `--headed`: launch headed; on an auto-replay that still fails the check, pause
  for manual completion (MFA/captcha), then re-snapshot and re-check.

Mirror `render`/`compile` command structure (`async_playwright` → launch →
`try/finally: browser.close()` → `asyncio.run`). `validate` accepts setup
scenarios for free (they are scenarios).

## Error handling

- Missing/stale cache, failing health-check, exceeded `maxAgeHours` → silent
  auto-refresh.
- Auto-refresh that still fails the check → **stop** (compile or render), with the
  disambiguated message above. Never produce a video/sidecar from a logged-out
  session **when a health-check is configured**.
- Setup scenario not compiled → stop, instruct `guidebot compile <setup>`.
- Recursion (`setup` on a setup source) → validation error.
- Auth values redacted in logs (existing `scenario_sensitive_values`); page text
  never logged.

## Security

- `.guidebot/sessions/*.json` = bearer credential → `0o600` file, `0o700` dir,
  self-writing `.gitignore` (`*`), never logged.
- Credentials continue to flow only through `${ENV}` substitution into the setup
  scenario's `enterText`/`navigate` values.

## Known limitations (documented, not solved in v1)

- **Cookies + localStorage only.** `sessionStorage`/IndexedDB-backed auth (some
  OIDC/MSAL SPAs) cannot be cached; the double-failure error names this.
- **Language-persisting sessions (review §6).** If the backend pins UI language
  to the session, reusing one language-agnostic session across localized variants
  can mismatch frozen `RoleTarget` labels. Symptom documented; key pre-wired for a
  later per-locale session.
- **Cross-origin** setup vs target hosts: hard-error (host guard), not supported.

## Testing (TDD)

Unit:

- Cache key stable; **changes with `env_digest`** (credential change → refresh,
  review §2) and with setup `config_hash`.
- Wrapper round-trips; `maxAgeHours` derives from `created_at`, not mtime.
- Reuse-vs-refresh decision table: present+check-pass → reuse; check-fail →
  refresh; `maxAgeHours` exceeded → refresh; no `verifyUserLoggedIn` + fresh →
  reuse (+ warning emitted); `--force` → refresh.
- `verifyUserLoggedIn` string shorthand parses to object.
- Recursion guard: `setup` on a setup source → validation error.
- Session file is `0o600`, written atomically; `.guidebot/sessions/.gitignore`
  auto-created.
- Health-check failure output contains neither tokens nor page text.

Integration (local HTTP server, mirroring `framing_server` +
`test_optional_branch_compile_render.py` patterns):

- **Target compile with a seeded session** — the load-bearing test: a target
  whose step-1 element exists only when logged in compiles successfully because
  `ensure_session` seeded the compile context (review §1).
- Setup replay "logs in" (server sets a cookie) → health-check passes → **render
  receives the session and the recorded video does not contain the login page**.
- Falsifying test: cache seeded with an expired cookie → health-check text absent
  → auto-refresh triggered.
- Health-check polling: text appears asynchronously → check still passes.
- `opens_popup` setup sidecar (SSO-style pop-up) → supported (replayed) or, if
  out of scope, a loud error — assert which.
- Pending gate in the setup sidecar whose element appears at replay → skipped/
  handled, login underneath still succeeds.
- Host mismatch (setup baseUrl host ≠ target baseUrl host) → hard error.
- `sessionStorage`-auth server → double-failure diagnostic message.
- Render-set: two variants, one shared cache → exactly one refresh, no races.

## Files to touch

- `models/config.py` — `Config.setup`; `verifyUserLoggedIn` (str | model);
  `maxAgeHours`; recursion validator; extend `config_hash` projection with
  `setup` + `env_digest`.
- `recorder/session.py` (new) — cache key/wrapper/load/save (atomic, perms,
  self-gitignore), health-check (polling, host guard), `ensure_session`,
  `RaisingReasoner`, `replay_setup` via the factored compile seam.
- `recorder/compile.py` — factor the context/replay seam to allow a caller-owned
  context + `storage_state` capture; seed target-compile context when
  `config.setup` set.
- `recorder/render.py` — call `ensure_session` before line 1705; thread
  `storage_state` into `new_context`.
- `cli.py` — `setup` command.
- `.gitignore` — `.guidebot/sessions/`.
- `examples/` — target scenario with `config.setup`; `verifyUserLoggedIn` on a
  login example.
- `docs/` (EN + PL) + README — setup reference, session cache, `guidebot setup`,
  the documented limitations.

## Build order (two phases, ONE PR)

The review recommends two PRs; the user wants a single PR, so build in two phases
and ship together.

- **Phase A** — `recorder/session.py` (key, wrapper, health-check, replay via
  compile seam), the `compile.py` seam factoring, `guidebot setup` CLI, schema
  fields + recursion validator, security hygiene, `.gitignore`. Independently
  testable; `guidebot setup` + a manual `storage_state` handoff already delivers
  value.
- **Phase B** — auto pre-phase wired into `run_render` and
  `run_compile_in_browser`, compile invalidation (`config_hash`), render-set
  interaction, examples + docs.

## Open questions

None blocking. Deferred by choice: `containsText` as a list; case-insensitive
matching; per-locale sessions; sessionStorage/IndexedDB capture.
