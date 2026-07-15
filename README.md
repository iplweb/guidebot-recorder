# guidebot-recorder

[![CI](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

Compile a text scenario (YAML) into a **deterministic training video**: a bot
opens a page, walks through a flow step by step (Playwright), shows the cursor and
clicks, and one or more language-specific voice-overs (TTS) narrate what happens.
The result is an `.mp4` with selectable audio tracks.

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
  (move, ripple, highlight), optionally draws a macOS-style browser bar and types
  navigation URLs into it, records video, and muxes the TTS narration into the final
  `.mp4`.

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
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true
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

### Multiple language audio tracks

Keep `config.tts` as the default language and add render-only `audioTracks` plus a
complete `translations` map on every narrated step:

```yaml
config:
  title: "Logging in"
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts:
    provider: edge
    voice: pl-PL-MarekNeural
    lang: pl-PL
    trackLanguage: pol
    title: Polski
  audioTracks:
    - provider: edge
      voice: en-US-GuyNeural
      lang: en-US
      trackLanguage: eng
      title: English
steps:
  - say: "Witaj."
    translations:
      en-US: "Welcome."
  - teach: "Kliknij przycisk Zaloguj."
    translations:
      en-US: "Click the Log in button."
```

`lang` is the TTS/translation key; `trackLanguage` is the lowercase ISO 639-2 code
stored in MP4 (`pol`, `eng`, `deu`, and so on). Every alternate track needs a
translation for every `say`/`teach`; Guidebot fails instead of silently mixing
languages. The canonical `teach` still controls compilation, so translated narration
cannot change the browser action and does not require a recompile.

Guidebot records the browser once. At each step it waits for the longest language,
then performs the shared action. The output contains one H.264 video stream and one
AAC-LC 48 kHz stereo stream per language; the default `tts` stream is first and
marked as default. Full-length WAV beds remain in
`<output-dir>/.guidebot_video/<output-stem>/bed-<trackLanguage>.wav`.
Successful rerenders replace that complete set and remove languages no longer
configured; a failed or interrupted build/mux keeps the previous master and beds.

The built-in `guidebot render` command uses Edge TTS and rejects any configured
provider other than `edge` before launching the recording browser. Custom callers of
the Python render API may inject another provider implementation, but all tracks in
one render must still use that same provider name.

The MP4 is a useful multi-track master. YouTube Studio's documented language workflow
currently asks for a separate audio-only file of roughly the same duration for each
added language, so use those generated WAV beds when uploading dubs:
[YouTube multi-language audio](https://support.google.com/youtube/answer/13338784?hl=en).
See [`examples/multilingual-login.scenario.yaml`](examples/multilingual-login.scenario.yaml)
for a complete scenario.

### Separate localized videos (render sets)

An embedded master intentionally keeps one canonical browser flow: `translations`
changes only alternate narration and never changes `teach` or another browser action.
When the page itself has a different locale, host, path, labels, or action wording,
use a localized render set made from complete scenarios instead.

```yaml
kind: localized-render-set
version: 1
variants:
  pl-PL:
    scenario: localized-login.pl-PL.scenario.yaml
    output: localized-login.pl-PL.mp4
  en-US:
    scenario: localized-login.en-US.scenario.yaml
    output: localized-login.en-US.mp4
```

The set CLI keeps the ordinary `render` command unchanged:

```bash
# Existing mode: one visual recording with selectable embedded audio tracks
uv run guidebot render examples/multilingual-login.scenario.yaml \
  --out out/multilingual-login.mp4

# Set mode: one independently compiled, single-audio MP4 per full scenario
uv run guidebot compile-set examples/localized-login.render-set.yaml
uv run guidebot render-set examples/localized-login.render-set.yaml \
  --output-dir out/localized-login
```

Scenario paths are relative to the manifest. Output paths are relative to the
required `--output-dir`. Each language owns its `locale`, `baseUrl`, `navigate`
values, narration, and canonical action descriptions; its normal compiled sidecar is
written beside that scenario. Every variant uses a fresh browser context and contains
exactly one audio stream. Preflight rejects path escapes (including symlinks and
Windows drive paths), colliding compiled sidecars, and MP4 paths that overlap another
variant's private render workspace.

Set commands run variants in manifest order and stop on the first error. Outputs from
earlier completed variants remain valid, the failed variant does not publish a
partial replacement, and later variants are not started. See the
[render-set manifest](examples/localized-login.render-set.yaml), the complete
[Polish](examples/localized-login.pl-PL.scenario.yaml) and
[English](examples/localized-login.en-US.scenario.yaml) scenarios, and the full
[localized render-set design](docs/superpowers/specs/2026-07-15-localized-render-set-design.md).

The optional `config.chrome` block is render-only and defaults to `enabled: false`,
so existing scenarios keep their current output. When enabled, `showUrl` controls
whether the address pill is visible and `typeOnNavigate` controls whether a string
`navigate` step types its URL character by character before loading it. Both default
to `true`. Appearance can be overridden with `height` (default `56`), `barColor`
(`#f3f4f6`), `textColor` (`#374151`), `radius` (`12`), `showLock` (`true`),
`closeColor` (`#ff5f57`), `minimizeColor` (`#febc2e`), and `maximizeColor`
(`#28c840`). These cosmetic settings stay outside the compile hash, so changing them
does not require recompilation.

`navigate` also accepts an object when one step should override the default:

```yaml
- navigate: "/login"                         # inherits typeOnNavigate
- navigate: { url: "/login", type: true }    # animate, then load
- navigate: { url: "/login", type: false }   # load, then update the pill instantly
```

`type` chooses animated versus instant display; it does not hide the URL. With
`showUrl: false`, the pill and typing delay are both disabled while the rest of the
bar remains visible. The injected bar reserves `height` pixels using top padding on
`<html>`. This intentionally changes the page's available layout area, but the video
remains exactly the configured `viewport` size: no desktop background or outer frame
is added. The displayed address is synchronized from `page.url` after navigation and
on the next `ensure`, not continuously through the History API. Because the complete
URL (including query and fragment) can appear in the video, disable `showUrl` for
secret-bearing URLs.

### Pop-up windows and new tabs

When a click opens a new Playwright page (a pop-up window or a target-blank tab),
Guidebot follows it automatically. Subsequent steps resolve and replay on that page;
after it closes, control returns to the main page. With `--headed`, the active page is
brought to the front in the visible Chromium window. The final video cuts between the
recorded page viewports (`main → pop-up → main`); native Chromium controls such as the
tab strip are not part of Playwright's video recording. When `config.chrome` is
enabled, its synthetic bar and URL pill are rendered inside both page viewports.

The first version supports one pop-up lifecycle per scenario and fails loudly on an
unexpected or second pop-up. Named tabs and explicit switch commands are deferred.
For a literal demonstration value, `teach` may infer typing and freeze that value in
the compiled sidecar. Passwords, tokens, and other secrets must continue to use
`enterText` with `${ENV_VAR}`.

## Status

Early (beta). The AI path (`compile` via Codex) and the real voice (edge-tts) are
implemented and unit-tested; the full test suite covers deterministic end-to-end
rendering (Playwright + ffmpeg) with a mocked resolver and a silent TTS provider.

## License

[MIT](LICENSE) © 2026 Michał Pasternak

[uv]: https://docs.astral.sh/uv/
[Codex CLI]: https://github.com/openai/codex
