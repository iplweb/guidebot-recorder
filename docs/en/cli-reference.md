# CLI reference

Run the CLI from the project environment:

```bash
uv run guidebot --help
```

The six public commands are:

| Command | Input | Result |
|---|---|---|
| `validate` | One source scenario | Schema validation only. |
| `compile` | One source scenario | One compiler-v2 sidecar. |
| `setup` | One setup scenario | A cached login session under `.guidebot/sessions/`. |
| `render` | One source scenario | One MP4 with one or more audio streams. |
| `guide` | One source scenario | One landscape PDF with annotated screenshots. |
| `compile-set` | Localized set manifest | One sidecar per stale variant. |
| `render-set` | Localized set manifest | One single-audio MP4 per variant. |

## `guidebot validate`

```bash
uv run guidebot validate PATH
```

Loads the source scenario, expands supported environment variables, and validates the
closed schema. It does not start Chromium, call an agent, inspect the target site, or
create a sidecar.

`PATH` is required. Success prints `OK` and exits with code 0. Missing environment
variables fail here even though no browser is used.

## `guidebot compile`

```bash
uv run guidebot compile PATH [OPTIONS]
```

Executes the scenario from the beginning in Chromium and writes the adjacent
`*.compiled.yaml` sidecar. The stock command always uses `CodexReasoner` for target
cache misses.

| Option | Default | Meaning |
|---|---:|---|
| `--headed` | off | Show the Chromium window. |
| `--force` | off | Ignore cached actions and resolve every target again. |
| `--pause-on-error` | off | On error, pause and keep a headed page available for inspection. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Show the progress bar, steps, reuse, and target summaries. |

`--timeout` controls Playwright. The built-in Codex reasoner has its own fixed
60-second timeout for each `codex exec` attempt and does not use this value. Retries
can make a single target take longer than 60 seconds overall.

If the artifact preflight confirms matching source name, compiler version, slot
alignment, command kinds, fingerprints, wait states, and relevant configuration, the
CLI prints that nothing needs compilation and does not open Chromium. Use `--force`
after an application, `navigate`, `baseUrl`, or account-state change that can alter
the page without changing a target fingerprint, or after an identity failure.

A normal first compile creates the required all-`null` sidecar for a nonempty
scenario containing only `say`, `navigate`, or numeric `wait`. A scenario with
`steps: []` produces no sidecar even with `--force` and cannot be rendered.

### Useful compile recipes

Normal incremental compile:

```bash
uv run guidebot compile scenarios/login.scenario.yaml
```

Visible diagnostic run:

```bash
uv run guidebot compile scenarios/login.scenario.yaml \
  --headed --pause-on-error --verbose
```

Full target rebuild:

```bash
uv run guidebot compile scenarios/login.scenario.yaml --force -v
```

Longer browser timeout:

```bash
uv run guidebot compile scenarios/login.scenario.yaml --timeout 45
```

Compilation uses a fresh context with the scenario's viewport and locale. The
synthetic browser bar remains render-only.

## `guidebot setup`

```bash
uv run guidebot setup SETUP_SCENARIO [OPTIONS]
```

Builds or refreshes the cached login session for a **setup** scenario, by
replaying it on a **non-recording** context. This is the manual entry point for
[pre-recording setup](scenario-reference.md#setup-verifyuserloggedin-and-maxagehours);
in the common case you never run it directly, because `guidebot compile` and
`guidebot render` of a target with `config.setup` auto-establish or reuse the
session on their own.

The setup scenario must already be compiled (`guidebot compile SETUP_SCENARIO`);
otherwise this command fails loudly and tells you to compile it. The replay makes
**zero LLM calls** — it only replays the setup's frozen targets.

| Option | Default | Meaning |
|---|---:|---|
| `--headed` | off | Show the browser. If the automatic replay's health-check fails, it pauses so you can finish logging in by hand (MFA/captcha), then snapshots. |
| `--force` | off | Always rebuild the session, ignoring any cache. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Show progress. |

A plain run behaves as check-and-reuse: with a live cache it prints
`session reused (already live)` and does not replay; otherwise it replays the
setup and prints `session refreshed and cached`. The session decision depends on
the setup's `verifyUserLoggedIn` and `maxAgeHours` — see the
[scenario reference](scenario-reference.md#setup-verifyuserloggedin-and-maxagehours).

The cached session is a bearer credential written `0600` (directory `0700`) under
`.guidebot/sessions/`, and the command auto-writes `.guidebot/sessions/.gitignore`
(`*`) so it is never committed.

## `guidebot compile-set`

```bash
uv run guidebot compile-set MANIFEST.render-set.yaml [OPTIONS]
```

Loads and preflights the manifest plus all referenced scenarios, then processes
variants in manifest order. Current compiler-v2 sidecars are reused; stale variants
are compiled in independent, locale-matched browser contexts. It stops on the first
failure, leaving earlier completed sidecars available and later variants untouched.

Options are the same as `compile`: `--headed`, `--force`, `--pause-on-error`,
`--timeout SECONDS`, and `--verbose`/`-v`. Here `--force` recompiles every variant.
If every variant is current, Chromium is not opened. There is no separate
`validate-set`; this command validates the manifest before browser use.

## `guidebot render`

```bash
uv run guidebot render PATH --out OUTPUT.mp4 [OPTIONS]
```

Loads the source and adjacent compiled sidecar, performs a strong compiler-v2
preflight before TTS/browser use, prepares every configured narration track, records
a fresh locale-matched Chromium session, verifies live ordinary-action identities,
follows one compiled pop-up lifecycle, and atomically publishes an MP4.

| Option | Default | Meaning |
|---|---:|---|
| `--out PATH`, `-o PATH` | required | Destination `.mp4` path. Parent directories are created. |
| `--headed` | off | Show the Chromium window while recording. |
| `--pause-on-error` | off | On error, pause and keep a headed page available for inspection. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Show TTS and render progress plus step errors. |
| `--auto-heal` | off | Reserved but not implemented; enabling it exits with an error. |
| `--hold-frame` / `--no-hold-frame` | unset — use `config.holdFrameForNarration` | Override the scenario's `holdFrameForNarration` for this run only. `--no-hold-frame` records every step's narration in real time, as before this feature existed; use it when a scenario's animations must keep running for the whole voice-over. Neither flag changes the config file. |
| `--hold-frame-settle FLOAT` | unset — use `config.holdFrameSettle` | Override `holdFrameSettle` for this run only, in seconds. Subject to the same minimum as the config field (two frames, `2/25` s). |
| `--dump-timeline` | off | Alongside the video, write the computed hold-frame timeline as `<name>.timeline.json`. Useful when the audio and video of a rendered file appear to drift, to inspect exactly where and how long the picture was held. |

Rendering makes no LLM calls. The stock command rejects any configured provider other
than `edge` before launching the recording browser. Tracks are configured in YAML;
there is no audio-track CLI flag. See [Multilingual audio](multilingual-audio.md).

Holding a frame for narration cuts render time roughly by the total length of the
voice-over, without changing the finished film's length or pacing — only its
recording time and, when the default is on, its look under narration (the page is
static instead of animating). See
[`holdFrameForNarration` and `holdFrameSettle`](scenario-reference.md#holdframefornarration-and-holdframesettle)
in the scenario reference for the full explanation.

There is no CLI flag for the synthetic browser bar. Enable it with `config.chrome`
in YAML. It is injected only by `render`; `compile --headed` never displays it.

### Useful render recipes

```bash
uv run guidebot render scenarios/login.scenario.yaml \
  --out out/login.mp4

uv run guidebot render scenarios/login.scenario.yaml \
  --out out/login.mp4 --headed --pause-on-error -v
```

For `--out out/login.mp4`, persistent work and language beds live under
`out/.guidebot_video/login/`. TTS cache entries live under `.guidebot/audio/`.

## `guidebot guide`

```bash
uv run guidebot guide PATH --out OUTPUT.pdf [OPTIONS]
```

Loads the source and adjacent compiled sidecar, then builds a landscape PDF guide with
one annotated screenshot per meaningful step, side-by-side narration text, and a visual
legend (arrows, circles, frames, glows).

| Option | Default | Meaning |
|---|---:|---|
| `--out PATH`, `-o PATH` | required | Destination `.pdf` path. Parent directories are created. |
| `--headed` | off | Show the Chromium window. |
| `--pause-on-error` | off | On error, pause and keep a headed page available for inspection. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Show page-build progress and step details. |

This command makes no LLM calls. Each guide page captures the frame at the moment an
interactive step (`click`, `hover`, `enterText`, `teach`) completes. `navigate` steps
produce a single text-only page. `slide` steps insert a visual section divider. `wait`
and `when` gates produce no output; a missing conditional element causes its whole branch
to be skipped.

Use `caption:` on a step to override the PDF text (falls back to `say` or `teach` if
omitted). See [Building step-by-step PDF guides](pdf-guide.md) for the full
explanation, limitations (single language, no pop-ups, no multi-step grouping), and
annotation legend.

## `guidebot render-set`

```bash
uv run guidebot render-set MANIFEST.render-set.yaml \
  --output-dir OUTPUT_DIR [OPTIONS]
```

`--output-dir` (alias `--out-dir`) is required. Manifest output paths are resolved
beneath it. Before TTS or browser use, Guidebot validates all scenarios, output and
workspace paths, and requires a current source-matched sidecar for every variant.
The stock command requires the set's common provider to be `edge`.

| Option | Default | Meaning |
|---|---:|---|
| `--output-dir PATH`, `--out-dir PATH` | required | Root for all manifest output paths. |
| `--headed` | off | Show Chromium while each variant records. |
| `--pause-on-error` | off | Keep a failing headed page available for inspection. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Show per-variant TTS/render progress. |

Variants render in manifest order and stop at the first error. Earlier completed
outputs remain valid, the failing output is not partially replaced, and later
variants are not started. See [Localized render sets](localized-render-sets.md).

## Environment variables per command

Guidebot reads the process environment; it does not load `.env` automatically:

```bash
DEMO_EMAIL=user@example.com DEMO_PASSWORD=replace-me \
  uv run guidebot validate scenarios/login.scenario.yaml
```

Use the same values for compile and render if page state or the resulting flow depends
on them.

## Documentation commands

Documentation dependencies are separate from runtime dependencies:

```bash
uv sync --group docs
uv run --group docs mkdocs serve
uv run --group docs mkdocs build --strict
```

The local site is bilingual: English is the root build and Polish is under `/pl/`.
