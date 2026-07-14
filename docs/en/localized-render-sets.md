# Localized render sets

Use a localized render set when each language needs a complete, independent browser
flow: a different host or route, page locale, labels, action descriptions, consent
steps, or final video. If only narration changes, one
[multilingual-audio MP4](multilingual-audio.md) is smaller and records the browser
only once.

## Files

Each variant is a normal, single-audio Guidebot scenario with its own compiled
sidecar. A versioned manifest groups them:

```text
scenarios/
├── login.render-set.yaml
├── login.en-US.scenario.yaml
├── login.en-US.compiled.yaml
├── login.pl-PL.scenario.yaml
└── login.pl-PL.compiled.yaml
```

```yaml
kind: localized-render-set
version: 1
variants:
  en-US:
    scenario: login.en-US.scenario.yaml
    output: login.en-US.mp4
  pl-PL:
    scenario: login.pl-PL.scenario.yaml
    output: login.pl-PL.mp4
```

See the complete
[manifest](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.render-set.yaml),
[English scenario](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.en-US.scenario.yaml),
and [Polish scenario](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.pl-PL.scenario.yaml).

Manifest variant keys are canonical BCP 47 language tags such as `en-US` and
`pl-PL`. Paths under `scenario` are relative to the manifest. Paths under `output`
are relative to the required `render-set --output-dir`.

## Variant requirements

Every referenced scenario is loaded during manifest preflight and must satisfy all
of these rules:

- `config.locale` equals its manifest variant key;
- `config.tts.lang` equals the same key;
- `config.tts.trackLanguage` is present and is a registered ISO 639-2 code;
- `config.audioTracks` is absent or empty: each variant produces exactly one audio
  stream;
- every variant uses the same configured TTS provider;
- scenario paths, compiled-sidecar paths, and output paths are unique.

Unlike alternate `translations`, each variant owns all its narration and canonical
action descriptions. Compile “Click Sign in” against the English page and “Kliknij
Zaloguj” against the Polish page instead of trying to translate a frozen target.

## Compile and render

```bash
export DEMO_EMAIL=user@example.com

uv run guidebot compile-set scenarios/login.render-set.yaml
uv run guidebot render-set scenarios/login.render-set.yaml \
  --output-dir out/localized-login
```

`compile-set` validates the whole manifest first, then processes variants in manifest
order. A current compiler-v2 sidecar is reused; a stale variant is compiled in its
own fresh Chromium context with the matching locale. `--force` recompiles every
variant.

`render-set` performs two full preflights before TTS or browser use:

1. all variant sidecars must be current and source-matched;
2. all output paths and private `.guidebot_video/<stem>/` workspaces must be safe and
   non-overlapping beneath `--output-dir`.

It then renders one single-audio MP4 per variant, in manifest order. The stock
command requires the common provider to be `edge`.

## Path safety

Manifest paths must be portable relative paths:

- scenarios end in `.scenario.yaml` or `.scenario.yml`;
- outputs end in `.mp4`;
- absolute paths, Windows drive paths, backslashes, colons, and `..` are rejected;
- a scenario symlink may not resolve outside the manifest directory;
- final output symlinks may not escape `--output-dir`;
- an output may not overlap another variant's output or private workspace;
- a generated compiled sidecar may not collide with another sidecar or the manifest.

These checks happen before Guidebot creates output directories or starts Chromium.
There is no separate `validate-set` command; both `compile-set` and `render-set` load
and validate the manifest.

## Failure and publication behavior

Set commands stop on the first failing variant. For compilation, earlier sidecars may
already have been updated and later variants are untouched. For rendering, earlier
completed MP4s remain valid, the failing variant keeps its previously published MP4
and audio bed when assembly fails, and later variants are not started.

Example output:

```text
out/localized-login/
├── login.en-US.mp4
├── login.pl-PL.mp4
└── .guidebot_video/
    ├── login.en-US/bed-eng.wav
    └── login.pl-PL/bed-pol.wav
```

The shared `.guidebot/audio/` cache and each output's private work directory persist
until removed manually. Treat them as potentially sensitive because they can contain
narration text, audio, and recorded application frames.

## Choosing the mode

| Requirement | Use |
|---|---|
| Same page and actions, selectable narration tracks | [Multilingual audio](multilingual-audio.md) |
| Different page locale, host, route, labels, or steps | Localized render set |
| One independently published video per language | Localized render set |
| One compact master with a shared visual timeline | [Multilingual audio](multilingual-audio.md) |
