# Optional branches — design

Date: 2026-07-20
Status: approved (revised after spec review against the codebase)

## Problem

Some parts of a scenario are genuinely conditional. The canonical case is a cookie
consent banner: it appears on one run and not on the next, depending on stored consent,
A/B bucketing or geography.

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

- Nested optional branches. Rejected by validation.
- `else` / alternative branches.
- Making `state: enabled` actually check enabled-ness (pre-existing limitation, documented
  at `docs/en/scenario-reference.md:486`).
- Removing the recording stall described under "Known limitations".

## Design

### 1. Scenario model

A **separate block model**, not a new field on `Step`. `Step` declares `extra="forbid"`,
so hanging `timeout` / `state` / `steps` off it would expose those keys on every step kind
and require extra cross-field validation. Instead:

```python
class WhenBlock(BaseModel):
    when: str                      # natural-language description of the gating element
    state: WaitState = "visible"
    timeout: float = 10.0          # matches WaitUntil.timeout
    steps: list[Step]              # plain Steps only — no nested WhenBlock
```

and `Scenario.steps: list[Step | WhenBlock]`.

```yaml
- when: "the cookie consent banner"
  timeout: 10
  steps:
    - teach: "click the button that continues to the site"
    - say: "We accept the cookies."

- teach: "click the account icon"     # always runs
```

The block key is `when`, not `optional`, deliberately: `optional` is taken by the
single-step shorthand below, and a key that is a bool in one position and a mapping in
another is a discrimination hazard in Pydantic and a readability hazard for users.

Single-step shorthand — a new field on `Step`, `optional: bool = False`:

```yaml
- wait:
    until: "the cookie consent banner"
    timeout: 10
  optional: true
```

Validation rules:

- `WhenBlock.steps` accepts `Step` only; a nested `when` is a validation error.
- `optional: true` is only meaningful on steps that resolve a target
  (`Step.requires_target()` is true) or on a numeric `wait`. On `say`-only, `navigate` and
  `slide` steps it is a validation error — silently accepting it would imply a guarantee
  we do not provide.
- The translation validator at `models/scenario.py:120-140` must recurse into
  `WhenBlock.steps`; otherwise children escape translation validation.

### 2. Flattening and the compiled artifact

`CompiledScenario.actions` is positionally 1:1 with scenario steps
(`models/compiled.py:15`). A block breaks that invariant, so we introduce:

```python
class FlatStep(NamedTuple):
    step: Step
    branch: int | None       # index of the owning WhenBlock, None at top level
    is_gate: bool            # True for the synthetic gate step of a branch
```

`Scenario.flat_steps() -> list[FlatStep]`. A `WhenBlock` contributes a synthetic gate step
— an ordinary `Step(wait=WaitUntil(until=block.when, state=block.state,
timeout=block.timeout))`, which the existing machinery (`command_kind`,
`requires_target()`, fingerprinting) already handles — followed by its children.

The `branch` field is load-bearing: both loops need to know which indices belong to which
branch in order to skip the children after a gate miss (render) or record them as pending
(compile). A bare `list[Step]` cannot express that.

**Call sites to migrate to flat indexing** (all currently iterate `scenario.steps`):
`render.py:722` (length validation), `:730` (fingerprint zip), `:736` (narration count),
`:744` (pre-synthesis), `:902` (progress bar), `:904` (main loop); plus the equivalents in
`compile.py`. Error messages of the form `step {index}` switch to flat indices; this must
be consistent everywhere or the numbering drifts.

`actions[i]` currently has two states: a `CachedAction`, or `None` meaning "this step needs
no target". A third is required — "needs a target, not yet resolved":

```python
class PendingAction(BaseModel):
    pending: Literal[True] = True
    fingerprint: Fingerprint
```

so `actions: list[CachedAction | PendingAction | None]`, discriminated on the `pending`
key. The fingerprint is retained so existing invalidation (`compiler_version`,
`config_hash`, `compiled_from`) keeps working.

Three cache paths need an explicit pending branch — they currently assume `CachedAction`
attributes and would raise `AttributeError`:

- `_compiled_action_is_current` (`render.py:650-683`) — compare fingerprint only.
- `_can_reuse` (`compile.py:460-473`) — `PendingAction` has no `expect`.
- `compile_up_to_date` (`compile.py:175-197`) — **policy: a pending entry counts as
  up-to-date.** Otherwise every `compile` launches a browser and burns the full gate
  timeout waiting for a banner that is optional by definition. `--force` re-attempts.

### 3. `compile` behaviour

When the gating element is absent, the compiler records a `PendingAction` for the gate and
for every child of the branch, warns on stdout, and the CLI exits 0. `run_compile` itself
simply returns normally; the exit code is CLI-level.

### 4. `render` behaviour

1. **Gate has a `CachedAction`** → `wait_for(...)`. `TimeoutError` means branch skipped:
   log it, skip every step whose `branch` matches, continue.
2. **Gate is `PendingAction`** → resolve in place. Because the canonical element appears
   *after a delay* (see `examples/onet-login.scenario.yaml:30` and the beta note in the
   docs), a single snapshot would produce spurious skips. The resolve therefore **polls**:
   re-run `collect_candidates` + resolve on an interval until the gate resolves or
   `block.timeout` elapses. On success, execute the children, resolving their pending
   actions as we go, and rewrite `.compiled.yaml`; the next render of that branch is
   deterministic. On timeout, skip the branch.
3. **Reasoner unavailable** → warn loudly, skip the branch, do not fail. Availability is
   probed with `shutil.which("codex")` rather than by string-matching the generic
   `RuntimeError` raised at `resolver/reasoner.py:80-85`.
4. `optional: true` on a step behaves the same, scoped to that one step.

Two plumbing changes this requires:

- `run_render` (`render.py:686-697`) takes `reasoner: Reasoner | None = None`; the CLI
  constructs one for render as it already does for compile (`cli.py:71,114`).
- The resolution logic currently inline in `compile.py:591-685` (`collect_candidates`,
  reprompt loop, `validate_compile_time`, `capture_identity`, `heuristic_expect`, teach and
  sensitive-value validation) moves into a shared module used by both loops. It must accept
  `Page | Frame`: `collect_candidates` is typed `Page`
  (`resolver/page_context.py:398-399`), but with chrome enabled the active render context
  is the page iframe's `Frame` (`render.py:1199`). Collecting from the chrome shell instead
  would yield the wrong candidates.

**Skipping a branch skips its children's narration and translations too.** Narration
segments are placed per index ahead of the action (`render.py:965-977`), so a skipped
branch must remove its segments from the timeline rather than leave silent gaps.

### 5. Error boundary

The load-bearing constraint. Only these signals count as "element absent":

| Path | Counts as absent |
|---|---|
| Gate, cached action | Playwright `TimeoutError` from `wait_for` |
| Gate, pending | Poll window elapses; resolver returned `no_action` / `no_handle` |
| Optional step, pending | Resolver returned `no_action` / `no_handle` |
| Optional step, cached | `reuse_is_valid` returns false (`render.py:1267`) |

Everything else fails the render, explicitly including **`multiple_actions`**
(`resolver/reasoner.py:27`). An ambiguous target description is an authoring error, not an
absent element; swallowing it would let a typo silently delete a branch from the video.

Errors *inside* a branch — a click failing on an already-resolved target, a navigation
error, a popup that never opened — still fail the render.

Without that line, `optional` degenerates into `except Exception: pass` and starts masking
real regressions. The existing codebase consistently avoids this: its tolerances are narrow
and justified in place (e.g. `compile.py:646`, where a click that closed the page is
swallowed because the click's intent demonstrably succeeded).

This distinction is honest but heuristic: a resolver that returns `no_handle` because the
page was mid-navigation is indistinguishable from one that returns it because the banner
was genuinely not there. We accept that, and it is why the tolerance is confined to
explicitly-marked optional branches.

## Known limitations

- **Recording stall.** `render` records wall-clock, so an in-place Codex call — up to 2×60 s
  (`reasoner.py:23`) — freezes a frame in the output video, and a missing banner costs
  `timeout` seconds of dead air. Accepted for now: the render that self-heals a branch
  produces a throwaway video, and every later render of that branch is clean. Cutting the
  stall out of the timeline is deferred to in-flight recording-pipeline work.
- **Popups inside an optional branch are unsupported.** A click resolved at render time
  carries no `opens_popup` observation from compile, so the render popup contract
  (`render.py:909`, `:1002`) will raise "unexpected popup". Document it; do not paper over
  it.
- The sidecar header says `GENERATED by 'guidebot compile'`
  (`scenario/compiled.py:17`); it is now also written by `render`. Update the wording.
  Note `write_compiled` (`scenario/compiled.py:66-88`) is a full atomic rewrite, not an
  append — the render path reuses it exactly as `checkpoint()` does in `compile.py:303`.

## Testing

Test-first, per project convention.

- **Models** — `WhenBlock` parses; nested `when` rejected; `optional: true` accepted on
  target-bearing steps and rejected on `say` / `navigate` / `slide`; `extra="forbid"` still
  rejects unknown keys; translation validation recurses into block children.
- **Flattening** — `flat_steps()` index alignment with and without blocks; `branch` and
  `is_gate` correctness; legacy sidecars without pending entries still load.
- **Compiled** — `PendingAction` round-trips through YAML; `_compiled_action_is_current`,
  `_can_reuse` and `compile_up_to_date` handle it without raising.
- **Render** — gate timeout skips the branch *and its children* while later steps still
  run; skipped branch drops its narration segments; gate present plus pending calls the
  Reasoner, polls, and rewrites the sidecar; missing Reasoner skips rather than fails;
  `multiple_actions` still raises; an error inside a branch still raises.
- **Compile** — absent element yields pending entries for gate and children, warns, returns
  normally.
- **Integration** — a local fixture page whose banner is toggled by a flag, and whose banner
  appears after a delay; the same `.compiled.yaml` rendered twice, once with and once
  without the banner.

This also closes a real coverage gap found during exploration: today **no test exercises
the execution path of `wait: {until: ...}` at all**, let alone its timeout. Integration
scenarios use only the numeric form of `wait`.

## Documentation

- `docs/en/scenario-reference.md` and `docs/pl/scenario-reference.md`: new section on
  optional branches, covering `when`, `optional: true`, the error boundary, and both known
  limitations.
- Amend the beta note at `docs/en/scenario-reference.md:486-492`, which advises adding a
  numeric `wait` before a conditional one. That workaround is unnecessary for optional
  branches.
- Rewrite `examples/onet-login.scenario.yaml` to use `when` — it is literally a cookie
  banner example.

## Rollout

Branch `feat/optional-branches`, worktree
`~/Programowanie/guidebot-recorder-feat-optional-branches`, merged via PR once CI is green.
