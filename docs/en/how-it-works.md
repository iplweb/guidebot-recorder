# How it works

Guidebot is a two-phase compiler with a generated sidecar as the trust and
reproducibility boundary.

```text
                         COMPILE
source YAML ─▶ fresh Chromium ─▶ candidate snapshot ─▶ Codex ─▶ validated target
     │                                                           │
     └──────────────────── generated *.compiled.yaml ◀────────────┘

                          RENDER
source YAML + compiled YAML ─▶ strong preflight ─▶ Chromium ─▶ video + audio track(s)
                                      no LLM call
```

## Validation

`guidebot validate` loads YAML, expands allowed environment variables, and validates
it with a closed Pydantic schema. Unknown keys and malformed steps fail. No browser
or agent is started.

Validation proves the document shape, not that the site or target elements exist.

## Compilation

After a browser-free artifact check, `guidebot compile` starts a fresh Chromium
context with the configured viewport and locale and executes the scenario from step
zero. This is necessary because each target must be resolved in the page state
created by all preceding steps.

For each step:

1. `say`, direct `navigate`, and numeric `wait` are executed without AI.
2. An element-based step checks whether its existing sidecar action can be reused.
3. On a cache miss, Guidebot collects current visible semantic candidates.
4. The reasoner returns structured action/target data only.
5. Guidebot builds a Playwright locator, requires exactly one compatible target,
   captures an independent identity, and executes the action. A safe literal
   `teach → type` value is frozen; a click-opened pop-up is detected automatically.
6. The aligned `actions` list is written atomically after every step, so partial
   compile progress survives a later failure. An existing sidecar may still retain
   stale, unprocessed later slots, and there is no completion marker; only a
   successful compile makes the artifact ready for render.

When several elements share a role and accessible name, the reasoner does not guess a
position. It names the candidate it means; trusted code then builds the locator, counts
the matches, and measures which one that candidate is, so a recorded `nth` index is
measured, not guessed. The reasoner can also narrow an ambiguous target with a `scope` —
an ancestor that contains distinguishing text — resolving the target to a single element
without relying on a position at all.

Compilation has real side effects: it navigates, fills, hovers, and clicks. It must
run against a resettable environment.

## The compiled sidecar

For `login.scenario.yaml`, Guidebot writes `login.compiled.yaml`. It contains:

- compiler schema version `2`;
- the source filename;
- one action slot for every source step;
- `null` for steps without a target;
- a structural target (`role`, `text`, `label`, or `testid`, optionally scoped);
- a frozen, locator-independent identity;
- readiness and fingerprint data;
- optional `input_text` for a safe literal `teach → type` action;
- `opens_popup: true` when the compiled click opened the supported pop-up.

The target is data, not an arbitrary selector expression. Trusted code builds the
Playwright locator; the generated file is never evaluated.

Commit the sidecar, but do not edit it manually.

## Incremental reuse

Guidebot may reuse an action at the same step index when its stored instruction and
selected config hash still match and the live element identity validates. Changing
viewport, locale, or TTS language changes that hash. Changing only narration usually
needs no browser compile.

There is also a browser-free CLI fast path. It requires a matching source filename,
compiler-v2 artifact and fingerprints, action count, null/non-null slot alignment,
command kinds, target instructions, wait state, readiness, and relevant config hash.
If all checks pass, `compile` exits before opening Chromium.

The fast path cannot inspect the live application. A changed `navigate`, `baseUrl`,
account state, or server route can lead to a different DOM without changing a target
fingerprint. Use `--force` after route/application drift or a render identity error.

## Rendering

`guidebot render` loads the source and its adjacent sidecar, then:

1. before TTS or browser use, verifies source provenance, compiler version, action
   count and kinds, fingerprints, wait states, and relevant config hash;
2. synthesizes every missing segment for every configured narration track;
3. opens a fresh Chromium context with the same viewport and locale as compile;
4. optionally injects the synthetic browser bar configured by `chrome`;
5. validates live identity before click, hover, and type actions, animates the
   cursor, and follows a compiled pop-up lifecycle;
6. records each active page viewport and composes `main → pop-up → main` when needed;
7. builds one full-length bed per language and muxes the MP4 atomically.

Narration for a step is played before that step's action. With several tracks they
start together and the longest language controls the shared pace. Render makes no
LLM calls, but may use network access for the target site and Edge TTS.

Conditional waits still skip the live frozen-identity comparison (`hidden` may have
no element), but their compiler-v2 fingerprint, action kind, and state are checked in
preflight. Render cannot detect a route-only or application-only drift before replay,
so recompile successfully after such changes. Alternate narration, `cursor`, and
`chrome` edits are render-only.

The browser bar exists only during render. Validation checks its schema, but compile
does not inject it and ignores `navigate.type`; even `compile --headed` shows the
ordinary page. During render, `type: true` types the resolved URL before `goto`, while
`type: false` loads first and then shows the final URL, including redirects.

## Pop-up lifecycle

A compiled click may open one new page. Guidebot marks that behavior in the sidecar,
makes the pop-up active for following steps, and returns to the main page after a
scenario action closes it. If it remains open, it stays visible through the end of
the video. Cursor and synthetic browser chrome are injected into both page viewports;
native Chromium tabs and window frames are not recorded.

The contract is deliberately narrow: one pop-up lifecycle per scenario, opened by a
click. A second, simultaneous, unexpected, late, or independently closing pop-up
fails loudly. There is no explicit switch-page command and iframe content remains
unsupported.

## Localized sets

`compile-set` and `render-set` orchestrate complete locale-specific scenarios in
manifest order. Each compilation gets a fresh locale-matched context and sidecar;
each render publishes an independent single-audio MP4. The entire manifest, all
scenarios, current sidecars, and output paths are preflighted before render starts.
See [Localized render sets](localized-render-sets.md).

## Drift detection

The structural locator says how to find an element. The separate identity says
whether the found element is still the element compiled earlier. Identity includes
the tag, optional test ID, optional link URL, and an ancestry digest.

If an ordinary click, hover, or type action is missing, ambiguous, or has a different
identity, render fails rather than asking an agent to repair the scenario
mid-recording. Run:

```bash
uv run guidebot compile path/to/flow.scenario.yaml --force
```

Then review and commit the new sidecar before rendering again.

## Repeatability boundary

The sidecar removes LLM calls from render, but identical video still depends on the
same target application state, network responses, account data, viewport, language,
fonts, browser behavior, and TTS availability. Pin and reset the test environment as
carefully as you would for an end-to-end test.
