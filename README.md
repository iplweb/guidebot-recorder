# guidebot-recorder

[![CI](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

Compile a text scenario (YAML) into a **deterministic training video**: a bot
opens a page, walks through a flow step by step (Playwright), shows the cursor and
clicks, and a voice-over (TTS) narrates what happens. The result is an `.mp4` with
narration.

## How it works — a two-phase compiler

You write the scenario as **plain-language intents** ("click Log in"). A separate
**compile** phase turns those into frozen, concrete element references and writes
them to a **separate `*.compiled.yaml`** next to your source — the source scenario
stays clean and readable. Rendering is then **deterministic** and needs no LLM: the
browser walks the whole flow in a single pass, identically on every run.

```
login.scenario.yaml ──compile (AI)──▶ login.compiled.yaml ──render (0×LLM)──▶ login.mp4
   (intents, yours)                      (frozen actions)                     (the film)
```

- **`compile`** — the only AI phase. The resolver (by default via [Codex CLI]) maps
  each instruction to a semantic Playwright locator, checks it is unique, and freezes
  the element's identity into `login.compiled.yaml`. Re-running is incremental: steps
  whose intent is unchanged are reused (no LLM); only new/changed steps are resolved.
  Editing only narration (`say`) needs no browser at all.
- **`render`** — no LLM. Reads the frozen actions, animates a synthetic cursor
  (move, ripple, highlight), records video, and muxes the TTS narration into the
  final `.mp4`.

Both the source and the compiled file are meant to be committed to git, so `render`
is reproducible in CI and a diff shows when a page changed and a reference drifted.

## Install

Requires **Python 3.12+**, [uv], **ffmpeg**, and the Chromium browser (Playwright).

```bash
uv sync
uv run playwright install chromium
# ffmpeg: macOS `brew install ffmpeg`, Debian/Ubuntu `apt install ffmpeg`
```

The `compile` phase uses [Codex CLI] by default (`npm i -g @openai/codex`) — it runs
on your subscription, no API key. The resolver is pluggable (the `Reasoner` interface).

## Usage

```bash
# 1. validate the scenario schema (no browser)
uv run guidebot validate examples/login.scenario.yaml

# 2. compile intents → login.compiled.yaml (AI phase)
uv run guidebot compile examples/login.scenario.yaml

# 3. render the deterministic voice-over video
uv run guidebot render examples/login.scenario.yaml --out out/login.mp4
```

Useful flags for `compile` and `render`:

| Flag | Effect |
|---|---|
| `--headed` | Show the browser window instead of running headless |
| `--verbose` / `-v` | Progress bar + per-step log |
| `--timeout <s>` | Playwright action timeout in seconds (default 15) |
| `--pause-on-error` | On failure, freeze the open window for inspection (headed) |
| `--force` | (`compile`) re-resolve every step, ignoring the cache |

### Scenario (source)

```yaml
config:
  title: "Logging in to the system"
  baseUrl: https://example.com
  viewport: { width: 1440, height: 900 }
  locale: en-US
  tts: { provider: edge, voice: en-US-GuyNeural, lang: en-US }
steps:
  - say: "Welcome. I'll show you how to log in to the system."
  - navigate: /login
  - teach: "To log in, click the Log in button in the top-right corner"
  - enterText: { into: "the email address field", text: "${DEMO_EMAIL}" }
    say: "Now I'm entering my email address."
  - wait: { until: "the loading spinner to disappear", state: hidden, timeout: 10 }
```

Commands: `say` (narration only), `teach` (the voice reads a whole guiding sentence
and the bot performs the action extracted from it), `enterText`, `navigate`, `wait`
(seconds or an element condition), plus `click`/`hover` as explicit escape hatches.
Substitute secrets with `${ENV_VAR}` — they never land in the repo.

## Status

Early (beta). The AI path (`compile` via Codex) and the real voice (edge-tts) are
implemented and unit-tested; the full test suite covers deterministic end-to-end
rendering (Playwright + ffmpeg) with a mocked resolver and a silent TTS provider.

## License

[MIT](LICENSE) © 2026 Michał Pasternak

[uv]: https://docs.astral.sh/uv/
[Codex CLI]: https://github.com/openai/codex
