# Getting started

This walkthrough creates, compiles, and renders one scenario. Run all commands from
the repository root.

## 1. Install the prerequisites

You need:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- Chromium installed for Playwright;
- ffmpeg and ffprobe;
- [Codex CLI](https://github.com/openai/codex) for `compile` and `compile-set`.

```bash
uv sync
uv run playwright install chromium
```

Install ffmpeg separately:

=== "macOS"

    ```bash
    brew install ffmpeg
    ```

=== "Debian / Ubuntu"

    ```bash
    sudo apt update
    sudo apt install ffmpeg
    ```

Install and authenticate Codex CLI:

```bash
npm install -g @openai/codex
codex login
codex login status
```

Codex CLI can reuse a ChatGPT sign-in or API-key authentication. A ChatGPT sign-in
does not require an API key; API-key use is billed through the API account. Guidebot
uses the authentication already stored by Codex CLI.

## 2. Create the source file

Use the recommended `name.scenario.yaml` suffix. For example, create
`scenarios/login.scenario.yaml`:

```yaml
config:
  title: "Logging in"
  baseUrl: https://staging.example.com
  viewport: { width: 1280, height: 720 }
  locale: en-US
  tts:
    provider: edge
    voice: en-US-GuyNeural
    lang: en-US

steps:
  - say: "I will show you how to log in."
  - navigate: /login
  - enterText:
      into: "the email address field"
      text: "${DEMO_EMAIL}"
    say: "First, enter the email address."
  - enterText:
      into: "the password field"
      text: "${DEMO_PASSWORD}"
    say: "Then enter the password."
  - teach: "Click the Log in button"
```

Use one main command per list item. `say` may stand alone or accompany an action.
See the [complete YAML reference](scenario-reference.md).

`teach` may also contain a literal demonstration value, for example “Type
`demo@example.com` into the e-mail field”. Compiler v2 can freeze that exact literal
as a `type` action. Use `enterText` plus an environment variable for passwords,
tokens, credentials, and all other sensitive values.

To add the optional synthetic browser bar during render, add:

```yaml
config:
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true
```

It is render-only and requires no CLI flag. If enabled, put the first `navigate`
before standalone introductory narration unless you want the initial `about:blank`
address visible during the intro. See [`chrome` configuration](scenario-reference.md#chrome).

## 3. Export value variables

Guidebot intentionally does not load `.env` files. Put variables in the process
environment:

```bash
export DEMO_EMAIL=guidebot@example.com
export DEMO_PASSWORD='replace-me'
```

Substitution is limited to string `navigate`, object `navigate.url`, and
`enterText.text`. A missing variable fails validation. Values are not written to the
source or compiled YAML, but text entered
into a normal field can still be visible in the final recording.

## 4. Validate without a browser

```bash
uv run guidebot validate scenarios/login.scenario.yaml
```

Success prints `OK`. Validation checks required fields, unknown fields, step shape,
and environment substitutions. It does not check whether the site or an element
exists.

## 5. Compile the element targets

Use a staging environment and a disposable account. Compilation really opens the
site and performs every step from the beginning.

```bash
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
```

This creates `scenarios/login.compiled.yaml` next to the source. The compiler opens a
fresh context with the configured viewport **and locale**, contains one action slot
per source step, and freezes targets for element-based steps. Compiler v2 also stores
literal `teach` typing and whether a click opened the one supported pop-up. Do not
edit the sidecar by hand.

If the browser fails on a step, preserve the window for inspection:

```bash
uv run guidebot compile scenarios/login.scenario.yaml \
  --headed --pause-on-error -v
```

A failed compile can leave a partial sidecar, including stale later slots from an
older run. It has no completion marker. Do not render after a failed compile; rerun
compilation successfully, using `--force` when in doubt.

## 6. Review and commit both YAML files

```text
scenarios/
├── login.scenario.yaml   # authored source
└── login.compiled.yaml   # generated, reviewed sidecar
```

Commit both. A later sidecar diff shows which target changed. Do not commit real
credentials or a production-only session.

## 7. Render the video

```bash
uv run guidebot render scenarios/login.scenario.yaml \
  --out out/login.mp4 --headed -v
```

Rendering:

- preflights the sidecar's source name, compiler version, slot count, action kinds,
  fingerprints, and relevant configuration before TTS or browser use;
- synthesizes every missing narration track into `.guidebot/audio/`;
- starts a fresh Chromium context with the same viewport and locale as compile;
- verifies live identities before click, hover, and text-entry actions;
- follows one pop-up opened by a compiled click and cuts `main → pop-up → main`;
- records work files under `out/.guidebot_video/login/`;
- muxes one or more narration streams into `out/login.mp4`.

No LLM is called during render. Network access may still be needed for the target
application and for Edge TTS on an audio-cache miss.

## 8. Recompile after change

Run normal `compile` after editing target instructions or relevant config. Use a full
rebuild when the application itself changed, after a Guidebot upgrade, or when render
reports an identity mismatch:

```bash
uv run guidebot compile scenarios/login.scenario.yaml --force --headed -v
```

The browser-free pre-check verifies compiler-v2 provenance, action alignment, command
kinds, fingerprints, and relevant configuration. It cannot inspect the current DOM or
prove that an edited `navigate`, `baseUrl`, account state, or server-side route still
leads to the same page. Use `--force` after those changes or any application drift.

## 9. Choose a localization mode

- Use [multilingual audio](multilingual-audio.md) for one browser recording with
  selectable narration streams.
- Use [localized render sets](localized-render-sets.md) when the page locale, host,
  route, labels, or actions differ and each language needs an independent MP4.
