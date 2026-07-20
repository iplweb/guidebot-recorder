# Optional branches — design

Date: 2026-07-20
Status: approved

## Problem

Some parts of a scenario are genuinely conditional. The canonical case is a cookie
consent banner: it appears on one run and not on the next, depending on stored
consent, A/B bucketing or geography.

Today such a step is a hard failure. `wait: {until: ..., state: visible}` maps to
`Recorder.wait_for` (`guidebot_recorder/recorder/recorder.py:141`), Playwright raises
`TimeoutError`, and nothing catches it at step level — the generic loop handler
(`render.py:1063`, `compile.py:429`) turns it into `RenderError` / `RuntimeError` and the
whole run dies. There is no `optional`, `soft` or `continue_on_error` field anywhere, and
`Step` declares `extra="forbid"`, so users cannot even express the intent.

A second, subtler problem: optionality is **not compilable ahead of time**. The compiler
resolves targets against a live page. If the banner did not show during `compile`, there
is no `CachedAction` to record. The current `render` is deliberately LLM-free, so a hole
in the sidecar can only be an error.

## Goals

- Express "this part may or may not happen" for a group of steps and for a single step.
- Absence of the element must not fail `compile` or `render`.
- A branch that was never compiled must still be executable when it does appear —
  resolved in place at render time and persisted so later runs are deterministic.
- Genuine errors must keep failing loudly.

## Non-goals

- Nested optional branches. Rejected by validation. No current use case, and allowing it
  forces recursion through step flattening and cache indexing.
- `else` / alternative branches.
- Making `state: enabled` actually check enabled-ness (a pre-existing limitation,
  documented in `docs/en/scenario-reference.md:486`).

## Design

### 1. Scenario model

A new block command, `when`, added to `PRIMARY_COMMANDS` in
`guidebot_recorder/models/scenario.py`:

```yaml
- when: "the cookie consent banner"   # natural-language description of the gating element
  timeout: 8                          # default 5.0
  state: visible                      # visible | hidden | enabled, default visible
  steps:
    - teach: "click the button that continues to the site"
    - say: "We accept the cookies."

- teach: "click the account icon"     # always runs
```

The block key is `when`, not `optional`, deliberately: `optional` is taken by the
single-step shorthand below, and a key that is a bool in one position and a mapping in
another is a discrimination hazard in Pydantic and a readability hazard for users.

Single-step shorthand — a new field on `Step`:

```yaml
- wait:
    until: "the cookie consent banner"
    timeout: 8
  optional: true
```

`optional: bool = False`. Validation rejects `when` blocks nested inside `when` blocks.

### 2. Compiled artifact

`CompiledScenario.actions` is positionally 1:1 with scenario steps
(`guidebot_recorder/models/compiled.py:15`). A block breaks that invariant, so we
introduce `Scenario.flat_steps()`: a linear sequence in which a `when` block contributes
one **synthetic gate step** (a `waitFor` action) followed by its children. Indexing stays
flat and the rest of the compiler is unaffected.

`actions[i]` currently has two states: a `CachedAction`, or `None` meaning "this step
needs no target". A third state is required — "needs a target, not yet resolved":

```python
class PendingAction(BaseModel):
    pending: Literal[True] = True
    fingerprint: Fingerprint
```

so `actions: list[CachedAction | PendingAction | None]`. The fingerprint is retained so
existing cache invalidation (`compiler_version`, `config_hash`, `compiled_from`) keeps
working unchanged.

### 3. `compile` behaviour

When the gating element is absent, the compiler records a `PendingAction`, prints a
warning to stdout, and **exits 0**. Compilation does not fail. Children of an unentered
branch are likewise recorded as pending.

### 4. `render` behaviour

1. Gate has a `CachedAction` → `wait_for(...)`. `TimeoutError` means **branch skipped**;
   log it and continue with the following steps.
2. Gate is `PendingAction` → resolve in place via the Reasoner. Not found → branch
   skipped. Found → execute the children, resolving their pending actions as we go, and
   **append the results to `.compiled.yaml`** using the existing atomic writer in
   `guidebot_recorder/scenario/compiled.py`. The next render of that branch is
   deterministic.
3. Reasoner unavailable (no Codex CLI) with a pending branch → loud warning, branch
   skipped, render does **not** fail.
4. `optional: true` on a step behaves identically, scoped to that one step.

### 5. Error boundary

This is the load-bearing constraint of the whole design.

Only **element absence** is tolerated: `TimeoutError` on the gate, and "resolver could not
find the target". Any error *inside* a branch — a click failing on an already-resolved
target, a navigation error, a popup that never opened — still fails the render.

Without that line, `optional` degenerates into `except Exception: pass` and starts masking
real regressions. The existing codebase consistently avoids this: its error tolerances are
narrow and justified in place (e.g. `compile.py:646`, where a click that closed the page is
swallowed because the click's intent demonstrably succeeded).

### 6. Testing

Test-first, per project convention.

- **Models** — `when` parses; nesting rejected; `optional: true` accepted; `extra="forbid"`
  still rejects unknown keys.
- **Compiled** — `PendingAction` round-trips through YAML; `flat_steps()` index alignment
  holds with and without blocks; legacy sidecars without pending entries still load.
- **Render** — gate timeout skips the branch and subsequent steps still run; gate present
  plus pending action calls the Reasoner and updates the sidecar; an error inside a branch
  still raises; missing Reasoner skips rather than fails.
- **Compile** — absent element yields a pending entry, a warning, and exit 0.
- **Integration** — a local fixture page whose banner is toggled by a flag; the same
  `.compiled.yaml` rendered twice, once with the banner and once without.

This also closes a real coverage gap found during exploration: today **no test exercises
the execution path of `wait: {until: ...}` at all**, let alone its timeout. Integration
scenarios only use the numeric form of `wait`.

### 7. Documentation

- `docs/en/scenario-reference.md` and `docs/pl/scenario-reference.md`: new section on
  optional branches.
- Amend the beta note at `docs/en/scenario-reference.md:486`, which currently advises
  adding a numeric `wait` before a conditional one. That workaround is no longer needed
  for optional branches.
- Rewrite `examples/onet-login.scenario.yaml` to use `when` — it is literally a cookie
  banner example.

## Rollout

Branch `feat/optional-branches`, worktree
`~/Programowanie/guidebot-recorder-feat-optional-branches`, merged via PR once CI is green.
