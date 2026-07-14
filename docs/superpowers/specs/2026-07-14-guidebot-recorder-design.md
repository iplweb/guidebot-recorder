# Guidebot-recorder — design (spec v4)

Date: 2026-07-14
Status: accepted for writing the implementation plan
Revision: v4 — after three rounds of self-review. Round 1: Fable + Codex (v1). Round 2:
Fable (v2). Round 3: Codex (v3). v4 = consolidation + normative data model (§4.3).
Changelog in §17.

## 1. Purpose and scope

A tool that, from a textual scenario description (YAML), generates a **training
video**: a bot navigates to a site, walks through a given feature step by step
(Playwright), shows the cursor and clicks, while a narrator (TTS) explains what is
happening. The output product is an **`.mp4` file with voice narration**.

The core idea is a **compiler**: a scenario written in human intentions
("click Log in") is **compiled** by AI into a form with frozen, concrete
element references. Thanks to this, the actual video rendering is
**deterministic in the action layer** (see §2, guarantee boundary) and does not
require an LLM: the browser walks through the entire feature from start to finish in
a single run, on a fresh session, without attaching mid-way.

### v1 scope
- The `compile` phase (intentions → compiled actions) with the AI resolver.
- The `render` phase (`.mp4` video with narrator).

### Designed but deferred (v-next)
- The `record` phase — recording the user's own clicks directly into the scenario
  (without AI). The step format and the data model are designed so that `record`
  plugs in later without rework.

## 2. Compiler model — two phases

```
[intention YAML]  --compile (AI/Codex)-->  ┐
[record (v-next)] --capture-->              ├─> [scenario.yaml with actions] --render--> [.mp4 video]
[manual edit]     -------------------------┘        (0×LLM, fresh browser, 1 pass)
```

### Determinism guarantee boundary
**Deterministic are: the actions, their types and element references** (frozen in
`cachedAction`) and **the content and length of the narration** (audio from cache, §8).
**Not guaranteed frame-by-frame:** page load times, network latency, CSS animations
of the target application. Render repeatability additionally relies on a pinned
**environment** (§16) and the scenario's **`config`** (viewport, language — §3.1).
"0×LLM" in render means no AI calls; it does not mean no network I/O to the
target application.

### The `compile` phase (`guidebot compile scenario.yaml`)
- Runs the scenario on a **fresh session** and executes the steps **sequentially from
  the start** (see the algorithm in §5.6), because the resolver needs a snapshot of the
  page *in the state of the given step*. All actions are really executed (side effects —
  see the requirements on the environment §16).
- For a step requiring a reference without a valid `cachedAction`, it calls
  **ElementResolver** (LLM/agent). The result — the reference structure + action type —
  is **written into the same file** under the given step as `cachedAction`.
- This is the **only** phase in which AI runs. The LLM **returns data only** (§5.5);
  all browser actions are performed by Playwright.
- File editing is **in place** (round-trip, §4).

### The `render` phase (`guidebot render scenario.yaml`)
- **Phase 0 — audio preparation (offline):** before we open the recorded
  browser, we synthesize and **cache the entire narration** (§8). Render does not call
  TTS "live".
- **0×LLM.** Reads the compiled `cachedAction`, replays the steps with plain
  Playwright. Before each action it **validates the reference against the live page**
  (§5.4, render-time): if the locator does not hit or the hit element does not match the
  frozen **identity** (`Identity`, §4.3) → **hard error** "re-compile" (render has no
  right to call the LLM).
- Fresh browser, everything in a single pass, no attaching mid-way.
- `--auto-heal`: **a reserved name, not implemented in v1** (error "not
  implemented"). Ultimately a separate repair command updates the cache and restarts
  the render from step zero — never an LLM during recording.

## 3. Scenario format (declarative YAML)

A scenario is `config` (§3.1) + a list of `steps`. YAML is the **authoring
format**; underneath there is a common Python API (`Recorder`, §6), and the YAML
runner is one frontend on top of it.

### 3.1 The `config` header
```yaml
config:
  title: "System login"
  baseUrl: https://app.example.com     # optional prefix for navigate
  viewport: { width: 1280, height: 720 }  # = video size; required for repeatability
  locale: pl-PL                        # Playwright context locale; feeds into configHash
  tts: { provider: elevenlabs, voice: "pl-PL-Marek", lang: pl-PL }
  chrome:                              # optional, render-only browser bar
    enabled: false                     # omit/false = no bar and no layout change
    showUrl: true                      # show the address pill
    typeOnNavigate: true               # type before goto unless the step overrides it
    height: 56
    barColor: "#f3f4f6"
    textColor: "#374151"
    radius: 12
    showLock: true
    closeColor: "#ff5f57"
    minimizeColor: "#febc2e"
    maximizeColor: "#28c840"
```
`viewport` is required — it determines both the repeatability of references and the
size of the `.mp4`. `locale` sets the browser context locale (and the application
language, if driven by locale/header) and feeds into `configHash` (§4.3).
`chrome` is a cosmetic, render-only block. Omitting it is equivalent to
`enabled: false`; when enabled, omitted `showUrl` and `typeOnNavigate` both default
to `true`, and the remaining fields use the values shown above. None of its fields
feeds into `configHash`, so changing the bar does not require recompilation.
**Unknown keys** in `config`, `chrome`, and steps = **hard error** (closed schema).

### 3.2 Commands

| Command | Meaning | Action | Reference (cache) |
|---|---|---|---|
| `say` | Pure narration, does nothing | — | no |
| `teach` | The narrator speaks a whole guide sentence; the LLM extracts an action from it and executes it | yes (inferred) | yes |
| `enterText` | Type text into a field (explicit value) | type | yes (on `into`) |
| `navigate` | Go to a URL | goto | no |
| `wait` | Time pause **or** a condition on an element | — | yes, if an element condition |
| `click` / `hover` | Explicit escape hatch (action without narration, or when narration ≠ action) | click/hover | yes |

**Step structure rules** (validated by pydantic, §12):
- **exactly one command per step** (error when e.g. `click` + `navigate` together);
- optional accompanying fields: `say` (custom narration for `enterText`/`click`/
  `hover`), `cachedAction` (added by compile).

`navigate` has two backward-compatible forms:
```yaml
- navigate: "https://www.onet.pl"                         # uses typeOnNavigate
- navigate: { url: "https://www.onet.pl", type: true }   # force animated typing
- navigate: { url: "https://www.onet.pl", type: false }  # force instant update
```
The object form is still the single `navigate` command. An omitted object `type`
inherits `config.chrome.typeOnNavigate`. `type` selects **animated versus instant**
display; it does not hide the URL. If `chrome.enabled` or `chrome.showUrl` is false,
typing is skipped and adds no delay. Compile always performs the same plain `goto`
and never installs the render-only browser bar.

**Variable substitution:** `${ENV_VAR}` is expanded **only in the value fields**
`enterText.text` and the string `navigate` / object `navigate.url`.
**Forbidden in narrative/instruction fields**
(`say`, `teach`, `enterText.into`, `wait.until`) — otherwise a secret could be
spoken by the narrator, land in the audio cache key (§8), the resolver prompt,
or `compiledFrom`. A literal `${` is written as `$${`. Expansion happens in
compile/render right before use — **secrets do not land in the repo**. A missing
variable → hard error.

### 3.3 `teach` — the workhorse
The value of `teach` is a **whole guide sentence** ("To log in, click the
Log in button in the top-right corner"). The narrator speaks it in full, while the
compiler:
1. **extracts the executable part** from the sentence,
2. **infers the action type** (click / hover),
3. **resolves the target** into a semantic reference (§5),
4. writes everything into `cachedAction`.

Limitations (the resolver contract must signal them):
- **0 actions in the sentence** (e.g. "Take a look at the panel") → compile error "use `say`";
- **>1 action** (e.g. "click A, then B") → compile error "split into steps";
- **a purely spatial instruction without a semantic handle** → see §5.5.

`teach` handles clicks/hovers. **Typing** is not done via `teach` (no explicit
value) — that is what `enterText` is for.

### 3.4 `wait`
Discriminated form:
```yaml
- wait: 2.0                              # seconds (no reference)
- wait: { until: "until the results table appears", state: visible, timeout: 10 }
```
The conditional variant compiles to a `cachedAction` with `action: waitFor` (§4.2)
and a frozen expected state (`state: visible | hidden | enabled`). It is an
**exception to render-time pre-validation** (§5.4): the element may not yet exist by
definition — identity validation happens **after** the condition is met; an exceeded
`timeout` → hard error.

### Example — after `compile` (the same file)
```yaml
config:
  title: "Login"
  viewport: { width: 1280, height: 720 }
  tts: { provider: elevenlabs, voice: "pl-PL-Marek", lang: pl-PL }
steps:
  - say: "Welcome. I'll now show you how to log in to the system."
  - navigate: https://app.example.com
  - teach: "To log in, click the Log in button in the top-right corner"
    cachedAction:
      action: click
      strategy: role
      role: button
      name: "Log in"
      exact: true
      identity: { tag: button, ancestryDigest: "h7f3…", identityVersion: 1 }
      expect: navigation
      fingerprint: { commandKind: teach, compilerVersion: 1, compiledFrom: "To log in, click the Log in button in the top-right corner", expect: navigation, configHash: "c19a…" }
  - enterText: { into: "email field", text: "${DEMO_EMAIL}" }
    say: "Now I'm typing in my email address."
    cachedAction:
      action: type
      strategy: role
      role: textbox
      name: "Email"
      exact: true
      identity: { tag: input, testid: "email", ancestryDigest: "a02c…", identityVersion: 1 }
      expect: none
      fingerprint: { commandKind: enterText, compilerVersion: 1, compiledFrom: "email field", expect: none, configHash: "c19a…" }
```

## 4. In-place compilation (single file)

- **Single file** — no separate "compiled" artifact.
- **Round-trip** via `ruamel.yaml`: the compiler **mutates the `CommentedMap`
  directly** (it does not pass the whole thing through the pydantic model on write),
  adding only the `cachedAction` key. Preserves formatting, ordering and comments.
- The **supported subset of YAML** is defined (block/flow, quotes) and covered by
  **golden-diff tests**; anchors/aliases are out of scope.
- **Atomic write:** a temporary file in the same directory → validation → `rename`.
- **Idempotency:** `compile` calls the LLM only for steps without a valid
  `cachedAction`. `--force` recomputes everything.
- **Staleness (drift) detection, §4.1.**

### 4.1 Fingerprint and drift
`cachedAction.fingerprint` contains: `commandKind` (kind of command), the target
fields (`compiledFrom`), `compilerVersion`, and `configHash` (a digest of the
relevant `config` fields, at minimum `viewport` and `tts.lang`). A step is
re-resolved when:
- the instruction text changed (`compiledFrom` ≠ current),
- the **command kind** changed (`click`→`hover` will not keep the old cache),
- `configHash` changed (e.g. viewport 1280→768 may hide the element into a menu),
- `compilerVersion` increased (a change in the reference schema).

**Note:** the fingerprint detects changes *in the scenario/config*, not *page
drift*. Page drift is guarded by **render-time validation** (§5.4), which compares
the frozen, **locator-independent identity attributes** (§4.2) of the hit element;
a mismatch → hard error "re-compile". Matching `role`/`name` alone is not
sufficient — the locator is built from them, so such a comparison would be
tautological.

### 4.2 The `cachedAction` schema (structural, versioned)
`action`: `click | hover | type | waitFor` — the frozen action type.

**Reference — a union discriminated by `strategy`** (pydantic), each strategy
carrying its own fields:
- `strategy: role` → `role`, `name`, `exact` (defaults to `true`), optionally `nth`.
- `strategy: text` → `text`, `exact`.
- `strategy: label` → `label`, `exact`.
- `strategy: testid` → `testid`.
- optional `scope` (for each strategy) = a **nested reference** with the same
  structure, narrowing the search to an ancestor subtree.

**Identity attributes** (frozen, locator-independent — for render-time validation
§5.4): `tag`, `testid` (if present), `href` (for links), the `ancestryDigest`
digest. They detect a swap of an element with the same accessible name.

**`waitFor`** additionally carries `state: visible | hidden | enabled` (§3.4) and is
exempt from existence pre-validation.

`fingerprint` (§4.1).

**No `locator` as an expression string.** The Playwright locator is built
**only in trusted code** from the structural fields — zero eval/parsing.

### 4.3 Data model — one source of truth (normative)
The types below are **one common pydantic model** referenced by: the Reasoner
output (§5.2), the `scope` field, `cachedAction`, and render-time validation. This
is the sole definition — sections §2, §5.4, §9 refer to it, they do not repeat it.
A change to the model → an increase of `compilerVersion`.

**`Target` (recursive, discriminated by `strategy`):** per-strategy fields as in
§4.2 (`role/name/exact/nth`, `text/exact`, `label/exact`, `testid`) + optional
`scope: Target` (the same structure, narrowing to an ancestor subtree). The
Reasoner's strict JSON covers **the entire** union.

**`Identity` (frozen identity, locator-independent):**
- `tag` (lowercase), `testid?`, `href?` (normalized to an absolute URL),
  `ancestryDigest` (SHA-256 of the list of `(tag, role)` of ancestors up to the root),
  `identityVersion`.
- **Equality (the single procedure, used in compile AND render):** all present
  fields equal + `identityVersion` equal. `role`/`name` are **not** a criterion (the
  locator is built from them — the comparison would be tautological).

**`cachedAction`:** `action` + a flattened `Target` + `Identity` + `expect` +
(`state` when `waitFor`) + `fingerprint`.

**`fingerprint`:** covers **all frozen fields that affect behavior**:
`commandKind`, `compiledFrom`, `expect`, `state` (waitFor), `compilerVersion`,
`configHash`. A change to any of them → re-resolve or a field refresh.

**`configHash`:** a canonical projection of `config` — `viewport.{width,height}`,
`locale` (§3.1), `tts.lang` — serialized with sorted keys → SHA-256;
`configHashVersion`. Render-only cosmetics (`cursor` and `chrome`, including
`chrome.height`) are deliberately excluded and never trigger recompilation.

**`expect`:** an optional field **accompanying the step in the source** (overrides
the compile heuristic) **and** copied into `cachedAction`. Source > heuristic;
feeds into `fingerprint`.

**`waitFor` lifecycle:**
- compile resolves `Target` in the state where the element **exists** (it waits for
  its appearance per the instruction), freezes `Target` + `Identity`;
- render waits for `state` up to `timeout`:
  - `visible`/`enabled` → after it is met, validates identity (§4.3 equality),
  - `hidden` → asserts **absence/being hidden**; identity validation is **skipped**
    (there is nothing to compare);
- `waitFor` is exempt from existence pre-validation (§5.4).

## 5. Resolver (only in `compile`, called rarely)

### 5.1 PageContext
Playwright extracts an **accessibility snapshot** of the current page and builds a
**limited list of candidates** (interactive elements + headings), each with:
a stable ID, `role`, accessible name, **bounding box**, ancestry (in brief),
visibility/enabled. The **pruning strategy** (viewport-only + interactive) keeps the
input size in check on large pages.

### 5.2 Reasoner (pluggable backend)
Maps `(candidates, instruction) → {action, Target}` (Target = the common model §4.3,
expressing every strategy `role/text/label/testid` + `scope`) or an **error signal**
(0 actions, >1 actions, no semantic handle). The strict JSON covers the entire
`Target` union. Selected in the config.
- **Default: `codex exec`** — subscription, zero API cost.
- Alternatives (deferred until the default works): `claude -p`, `opencode`, the Claude
  Messages API.

### 5.3 The `codex exec` call contract (§5.2 default)
- the call is **pinned, read-only / no file tools** (the agent only reasons
  over text),
- input: a **redacted** candidate snapshot (no secrets/field values),
- output: **strict, framed JSON** per the schema (framed markers), parsed
  rigorously; separate `stderr`,
- **timeout + cancellation**, a **bounded number of attempts**,
- resistance to prompt injection: text from the page is *data*, not an instruction.

Default mechanism: **snapshot→agent (text)**. **CDP-attach** (interactive
investigation of the page by the agent) — deferred until the default path works.

### 5.4 Trust-but-verify (two levels)
**Compile-time** (before writing to cache): the hit locator must:
- hit **exactly 1** element (`exact: true` by default — protects against the
  substring match of `get_by_role(name=)`),
- be **visible** and **enabled/editable** as appropriate for the action,
- have a **type consistent with the action** (e.g. `type` only on a `textbox`).
Failure → **re-prompt** (max 2 attempts), then a **hard error** with a list of
candidates for the author to disambiguate.

**Reuse of an existing `cachedAction`** (§5.6) additionally validates **`Identity`
equality** (§4.3), not just uniqueness/type. A replacement hit by the same locator
but with a different identity → treat it as a cache miss and **re-resolve**
(otherwise render would keep getting "re-compile" over and over).

**Render-time** (§2): before an action the locator must hit 1 element, and its
**identity attributes** (`tag`/`testid`/`href`/`ancestryDigest`, §4.2) must match the
frozen ones (comparing `role`/`name` alone would be tautological — the locator is
built from them). A mismatch → hard error "re-compile".
**`waitFor` exception:** existence pre-validation is skipped; we wait for `state` up
to `timeout`, and only then validate identity — a timeout → hard error.

### 5.5 The LLM's role — boundary and action execution
The LLM/agent runs **only in `compile`** and **returns data only** (reference + type).
It **never** drives the browser — validation and all actions (compile and render)
are performed by Playwright. Purely spatial instructions without a semantic handle
(e.g. just "in the top-right corner" with no name) are resolved by the resolver via
candidate geometry (§5.1) into a reference with `nth`/`scope`; if that is not
possible — an **explicit error** "unsupported instruction, please clarify".

### 5.6 The `compile` algorithm
```
open a fresh session; set viewport from config
for each step in order:
  say                        → no-op (narration counts only in render)
  wait: N (seconds)          → perform the pause (needed for the page to reach its state)
  navigate                   → perform goto (Playwright)
  step requiring a reference (teach / enterText / click / hover / wait:until):
     if cachedAction is valid per the fingerprint AND passes compile-time
        validation on the live page → use it
     otherwise (missing / drift / DOES NOT HIT on today's page):
        collect candidates (PageContext)
        Reasoner → data; validate compile-time (§5.4); re-prompt/error
        write cachedAction to the file (atomically, §4)
  perform the action with Playwright (to expose the state for subsequent steps)
  apply the readiness rule (§7.1) before the next step
```
**Crucial:** "valid fingerprint" is not enough — if the frozen reference **does not
hit on the current page**, compile treats it as a cache miss and **re-resolves**
(otherwise compile would fail with an absurd "do a compile"). The compile phase is
the only place where the LLM may be called.

## 6. The `Recorder` engine (Python API) and frontends

- **`Recorder`** — the only place that "knows how": `navigate / say / enter_text /
  click / hover / wait`. The core.
- **YAML runner** — iterates the steps and calls `Recorder`; handles `teach` and the
  compiled `cachedAction`.
- **Python API (v1):** accepts **only explicit, structural references**
  (`click(role="button", name="Log in")`). It has **no** `teach`/LLM resolution
  nor in-place cache — those are exclusively the YAML+compile path. (A full scripting
  frontend with reference freezing — deferred.)

## 7. Cursor, click, and browser chrome visualization — DOM overlays

Playwright drives programmatically and **does not render** the cursor. We inject an
**artificial cursor** (HTML/SVG) + animations: smooth movement to the target, a
"ripple" on click, element highlight.

- **Overlay only in `render`** (in compile it would pollute the accessibility snapshot).
- **Re-inject on every navigation:** `add_init_script`, because a full pass
  destroys the DOM. The cursor position is kept **on the Python side** and restored
  after the new document loads.
- **Re-check before each step:** an SPA rerender may replace a DOM subtree (along
  with the cursor) **without** navigation, so before each step we cheaply check for the
  cursor's presence and re-inject it if needed.
- **`pointer-events: none`** on the overlay (otherwise it would intercept the bot's
  clicks); no layout impact.
- Off-screen element: **first scroll to the target and wait for a stable bounding
  box**, only then move the cursor + ripple at the **moment of the real action**.

### 7.1 Readiness rule
Every action step carries a frozen `expect: navigation | idle | none` field (added
by compile to `cachedAction`; the compile heuristic: comparing the URL after the
action + `networkidle`, with the option to override in the scenario). Behavior after
the action:
- `navigation` → `wait_for_load_state` after the transition,
- `idle` → `wait_for_load_state('networkidle')` (SPA rerender without navigation),
- `none` → only a short settle.
After `navigate`, always `navigation`. This is an **explicit completion contract** —
without guessing: it determines the page state seen by the next step's resolver
(compile) and the stability (render).

### 7.2 Browser chrome and URL typing

When `config.chrome.enabled` is true, render installs a second DOM controller beside
the cursor `Overlay`. It draws a fixed, macOS-style top bar with traffic-light dots
and, when `showUrl` is true, an address pill. The bar has `pointer-events: none` and
a high z-index below the synthetic cursor, so it is visible without intercepting
actions. `showLock` displays a lock only for an `https:` address.

The controller uses the same lifecycle as the cursor: an init script installs it in
future documents, and render calls `ensure` after navigation and before recorded
steps to recreate DOM removed by a full load or SPA rerender. With `showUrl` enabled,
`ensure` also synchronizes the pill from Playwright's current `page.url`. This catches
a click/SPA URL change at the next ensure; there is intentionally no live
`pushState`/`replaceState`/`popstate`/hash observer in this version.

For a `navigate` step, the effective typing flag is the object's `type` when present,
otherwise `config.chrome.typeOnNavigate`:

1. If effective typing and `showUrl` are true, `set_url(target, animate=True)` types
   character by character in JavaScript. Python awaits its Promise before `goto`.
2. Playwright performs the real navigation.
3. Cursor and chrome are re-ensured; chrome synchronizes the final `page.url`,
   including redirects. With effective typing false, the URL appears instantly only
   after navigation. With `showUrl: false`, no URL or artificial typing delay occurs.

The bar reserves `chrome.height` pixels by applying top padding to `<html>`, keeping
normal page content below it. This is an explicit layout compromise: it may affect
responsive or fixed-position content, but it does not expand the recording canvas.
The output remains a rectangular video exactly equal to `config.viewport`; there is
no four-sided browser frame, desktop background, or rounded lower window corners.
Changing any chrome setting is cosmetic and outside `configHash`.

The pill displays the complete URL, including query and fragment. Scenarios whose
URLs contain credentials or tokens must use `showUrl: false`; automatic redaction is
out of scope.

## 8. Narration (TTS) and audio assembly

- **Pre-cache (render Phase 0):** before opening the recorded browser, we
  synthesize and **validate** each narration segment, saving it to cache (a build
  directory, e.g. `.guidebot/audio/<hash>.wav`; **key = hash: the full `config.tts`
  section (provider, voice, lang, model/speed) + text + `ttsAdapterVersion` +
  `cacheSchemaVersion`** — even an adapter/provider upgrade with an unchanged
  `config.tts` also invalidates the cache). Render reads from cache → no network
  calls and no "dead frames" during recording; a TTS failure surfaces **before**
  render.
- **Timing model — narration drives the pace:** the length of each segment `T` is
  **known from cache** before playback. A step with narration: speak (start audio) →
  wait `T` → perform the action.
- **Assembly (K2 — Playwright video + audio bed):**
  - render records built-in video (`context.record_video`, WebM VFR),
  - segment offsets are anchored to **a single monotonic clock** whose
    **zero point = the first video frame** (not context creation, which happens
    earlier — otherwise the whole narration would have a constant offset); the anchor
    is established by `ffprobe` (`start_time`/PTS) or a visual marker in the first
    overlay frame (choice in §16),
  - after the context closes we **probe** the final video (ffprobe: length),
  - we build an **audio bed** = filler silences + segments at the computed offsets,
  - **ffmpeg** mixes the bed with the video with explicitly specified: sample rate,
    codecs, `-shortest`/pad at the end, trim/pad to the video length.
  - **A conscious tradeoff:** WebM VFR gives **approximate** sync (not
    frame-accurate); acceptable, because the pace is dictated by the narration and the
    `T` pauses. Exact post-sync is deferred (§14).
- **`enterText`/action without `say`:** a short, **configurable** pause (default e.g.
  0.5 s) instead of full audio-driven silence.

## 9. Flow of a single `teach` step (render)
```
step: teach: "To log in, click Log in in the top-right corner"
       cachedAction: {action: click, strategy: role, role: button, name: "Log in", exact: true}

RENDER (audio already in cache, length T known):
1. overlay: (optional caption/bubble), start the segment audio
2. wait T                                     ← narration drives the pace
3. build the locator from the cachedAction fields (trusted code)
4. render-time validation: 1 hit + identity match (`Identity`, §4.3/§5.4) — else error
5. scroll to the target, wait for a stable bbox
6. overlay: cursor movement + ripple + highlight at the moment of the action
7. Playwright performs cachedAction.action (click)
8. readiness rule (§7.1); segment offset recorded to the audio bed (§8)
```

## 10. Artifacts and project layout
```
my-training/
  login.scenario.yaml      # source + compiled actions (single file, in git)
  .guidebot/audio/         # TTS cache (build, outside git)
  out/login.mp4            # generated by `render`

guidebot_recorder/         # application package (uv + pyproject)
  scenario/    # schema (pydantic) + loader + round-trip (ruamel) + ${ENV} + config
  recorder/    # Recorder (Python API) + YAML runner + readiness rule
  resolver/    # PageContext (candidates+geometry) + Reasoner (codex/...) + validation
  overlay/     # injected JS: artificial cursor + animations (re-inject)
  chrome/      # injected JS: optional browser bar + URL typing (re-inject)
  tts/         # TTS interface + providers + cache
  video/       # recording + audio bed + mux (ffmpeg/ffprobe)
  cli.py       # compile / render / validate
```

## 11. Error handling (fail-loud, never silently)
- **compile — 0/>1 candidates, wrong identity, inconsistent type:** re-prompt (max 2)
  → hard error + candidate list.
- **compile — `teach` 0/>1 actions / unsupported instruction:** error with a hint.
- **render — missing/inconsistent `cachedAction`, locator does not hit or wrong identity:**
  hard error "re-compile".
- **TTS failed:** error in Phase 0 (before recording), not a silent video without voice.
- **`${ENV_VAR}` missing:** hard error.
- **Playwright navigation/timeout:** we propagate it.
- **`--auto-heal` in v1:** error "not implemented".

## 12. Tests
- **Unit:** schema/loader + `${ENV}`; round-trip (golden-diff: adding
  `cachedAction` preserves comments/ordering/the YAML subset, atomic write);
  Reasoner with a **mocked agent**; compile-time validation (uniqueness, exact,
  visibility/enabled, type consistency); fingerprint/drift; the "one command
  per step" validator; string/object `navigate` and chrome defaults/hash exclusion.
- **DOM overlay (real headless Chromium):** chrome injection and computed
  `pointer-events`/z-index, URL text, awaited typing to the complete string,
  re-injection after navigation, recreation after an SPA wipe, URL synchronization
  on `ensure`, and `<html>` top padding equal to the configured height.
- **Integration:** static **HTML in the repo** + Playwright → `compile` + `render`.
  **Strong** assertions, **not just "the mp4 exists"**: a trace of the executed actions,
  the identity of the clicked element, the presence of the cursor in sampled frames,
  the audio offset and length within bounds, a repeated render under a pinned
  environment yields a consistent result.
- **CI:** the LLM/agent is **always mocked**; the real resolver only in an
  "on-demand" test.

## 13. Stack
Python 3.12+, `uv`, Playwright (Python), `pydantic`, `ruamel.yaml`, `typer`,
`ffmpeg`/`ffprobe`. TTS and Reasoner behind interfaces (pluggable backends).
**External dependency:** the default Reasoner (`codex exec`) requires an installed
Codex CLI (`npm i -g @openai/codex`); its absence = a **readable configuration error**
with a hint to install it or to point to another backend (never a silent fallback).

## 14. Deferred (YAGNI)
- The `record` phase (recording one's own actions) — designed, not implemented.
- A full Python frontend with reference freezing (v1 = explicit locators only, §6).
- The "Python inside YAML" hybrid.
- `--auto-heal` (reserved, "not implemented").
- Additional Reasoner providers + CDP-attach (until the default `codex exec` works).
- Multi-tab / iframe scenarios.
- Live browser-URL observation through the History API, `popstate`, and hash events;
  this version reconciles `page.url` only on explicit navigation and `ensure`.
- A four-sided browser/window frame, desktop background, rounded lower corners, or
  an ffmpeg-expanded canvas; the video remains exactly the configured viewport.
- Exact audio post-sync (stretching to markers, frame-accurate sync).
- Masking of sensitive values on screen/captions (v1 protects only the repo via
  `${ENV_VAR}`), including automatic redaction of URL query strings/fragments.
- Dictating narration during `record`.

## 15. Target environment — requirements (regarding `compile` and repeatable `render`)
`compile` and `render` **really execute actions** on the target application (login,
entries). Therefore:
- an indicated **test account/environment** with a **resettable state** (fixtures),
- input data via `${ENV_VAR}` (§3.2),
- a **pinned** viewport/language (`config`) for the repeatability of references and
  video size.
Without a resettable state, re-compiling a middle step may depend on the effects of
earlier ones — the §5.6 algorithm always replays from step zero on a fresh session.

## 16. Matters to be settled during implementation
- The concrete TTS provider to start with (interface pluggable).
- The exact JSON framing format in `codex exec` and snapshot redaction.
- Overlay defaults (cursor speed, ripple/highlight style, pause for actions
  without `say`) — with the option to override in `config`.
- ffmpeg parameters (codecs, sample rate) and the threshold of acceptable sync drift.

## 17. Changelog (v1 → v2, after self-review)
- **K1:** added TTS pre-cache (Phase 0), render without live TTS calls.
- **K2:** chose the assembly mechanism (Playwright video + audio bed, approximate
  sync); added anchoring to a monotonic clock, probe, trim/pad.
- **K3:** `cachedAction` structural/versioned; removed the `locator` string.
- **I1/§15:** added requirements on the environment and compile side effects.
- **I2/§5.5:** clarified — the LLM returns data only, Playwright executes.
- **I3/§4.1:** fingerprint (command kind + version) + render-time drift validation.
- **I4/§5.4:** `exact: true` by default + visibility/enabled/type consistency + identity
  assertion in render.
- **I5/§5.1,5.5:** candidates with geometry/ancestry; handling/rejection of spatial
  instructions.
- **I6/§5.3:** the `codex exec` contract (read-only, framed JSON, timeout, retry,
  redaction).
- **I7/§3.4:** `wait` = time + element condition (with cache).
- **I8/§7:** overlay re-inject, `pointer-events:none`, scroll+stable bbox.
- **I9/§3.1:** the `config` header (viewport required).
- **I10/§4:** `CommentedMap` mutation, YAML subset, atomic write, golden-diff.
- **I11/§5.1:** snapshot pruning.
- **I12/§6:** Python API v1 with explicit locators only.
- **Secrets/§3.2:** `${ENV_VAR}` substitution.
- **Minor:** re-prompt limit; `teach` 0/>1 actions; "one command per step";
  `--auto-heal` "not implemented"; stronger integration tests.

### Changelog (v2 → v3, after the second round — Fable; Codex round-2 did not start: CLI not installed)
- **§4.2/§3.4:** added `action: waitFor` + `state`; `wait:until` has a place in the
  schema and is exempt from existence pre-validation.
- **§4.2:** `cachedAction` as a **union discriminated by `strategy`** (role/text/
  label/testid) + the definition of `scope` (nested reference).
- **§4.2/§5.4:** added **identity attributes** independent of the locator
  (`tag`/`testid`/`href`/`ancestryDigest`) — the end of tautological render-time
  validation (which compared `role`/`name`, from which the locator is built).
- **§5.6:** an explicit **re-resolve** branch when the fingerprint is "valid" but the
  reference does not hit on the current page; `wait:N` executed in compile, `say` a no-op.
- **§4.1:** the fingerprint covers `configHash` (viewport/lang).
- **§7.1:** a defined completion contract `expect: navigation | idle | none`.
- **§7:** cursor re-check before each step (SPA rerender without navigation).
- **§8:** TTS cache key = the full `config.tts` section + text; a named zero point of
  the monotonic clock (the first video frame).
- **§3.2:** `${ENV_VAR}` only in value fields, forbidden in narration/instruction,
  escape `$${`.
- **§13:** noted the dependency on the Codex CLI for the default Reasoner.

### Changelog (v3 → v4, after the third round — Codex on v3)
Consolidation: the nature of the remarks shifted from design flaws to inconsistencies
within the spec (v3 spot-fixes were not propagated everywhere). Class-level closure:
- **§4.3 (new, normative):** one source of truth — `Target` (a common union for the
  Reasoner, `scope`, `cachedAction`), `Identity` (canonical equality), `fingerprint`
  (all frozen fields that affect behavior: `expect`, `state`), `configHash`
  (canonical projection + version), the `waitFor` lifecycle.
- **Identity propagation:** §2, §9 and the examples in §3.2 now refer to
  `Identity` (§4.3), not to the tautological `role`/`name`.
- **§5.2:** the Reasoner contract returns the full `Target` union (not just `role/name`).
- **§5.4:** cache reuse validates **`Identity` equality** → the end of the
  "render→re-compile→render" loop on element swap (Codex's NEW-DEFECT).
- **§3.1:** added `config.locale` (browser locale, in `configHash`); unknown
  keys = hard error (closed schema).
- **§8:** the TTS cache key with an adapter/schema version salt.
- **Remaining precision** (the exact normalization of `href`/`testid`, the
  `ancestryDigest` inputs, the full YAML grammar) deliberately moved to the
  **implementation plan** — Pydantic + TDD will pin it down verifiably (diminishing
  returns from further prose).
