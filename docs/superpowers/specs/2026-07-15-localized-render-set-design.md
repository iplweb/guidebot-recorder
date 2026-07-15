# Localized render sets — design

**Date:** 2026-07-15

**Status:** implemented contract

**Extends:** `2026-07-14-guidebot-recorder-design.md` and
`2026-07-15-multilingual-audio-design.md`

## Goal

Produce one conventional, single-audio MP4 per language when the page itself must be
localized: browser locale, host or path, visible labels, action descriptions, and
narration may all differ. Each variant is a complete normal Guidebot scenario with
its own compiled sidecar. A small manifest groups those scenarios into one ordered
`compile-set` / `render-set` job.

This is a second output mode, not a replacement for the embedded multi-audio master:

- `guidebot render scenario.yaml --out film.mp4` keeps its current behavior. It
  records one canonical browser flow and can embed all configured `audioTracks` in
  that one MP4.
- `guidebot compile-set manifest.yaml` and `guidebot render-set manifest.yaml
  --output-dir out` operate on complete localized scenarios. They record and encode
  one independent video per variant, each with exactly one audio stream.

Use an embedded master when every narration describes the same UI. Use a localized
render set when the UI, URL, action intent, or natural pacing differs by language.

## Why full scenarios

The existing `steps[].translations` field has a deliberately narrow contract: it is
alternate narration for an embedded audio track. It is render-only, is never sent to
the Reasoner, and cannot change browser behavior.

Reinterpreting a translated `teach` as an action only in some commands would make the
same YAML mean two different things. It would also leave ambiguous rules for
`click`, `hover`, `wait.until`, `enterText.into`, language-specific navigation, and
steps that combine `say` with an action.

A render set therefore uses complete scenarios. In an English scenario, the English
`teach` is both English narration and the English compile-time action intent. An
English `click`, `hover`, `wait.until`, or `enterText.into` is likewise compiled
against the English page. There is no action overlay and no automatic translation
fallback.

## Manifest contract

The manifest is YAML with an exact kind, schema version, and an ordered mapping from
BCP 47 language tag to scenario and output paths:

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

Rules:

- `kind` is exactly `localized-render-set`; `version` is the integer `1`.
- `variants` is a non-empty mapping. Its YAML order is the execution order.
- Every key is a unique tag in the canonical BCP 47 shape supported by v1: a
  two- or three-letter language, optionally followed by script, region, and variant
  subtags.
- `scenario` is a relative path ending in `.scenario.yaml` or `.scenario.yml`.
  It is resolved relative to the directory containing the manifest.
- `output` is a relative `.mp4` path. It is resolved underneath the explicit
  `--output-dir`, not beside the manifest.
- POSIX absolute paths, Windows drive paths, and relative paths that escape their
  root with `..` are rejected. A scenario symlink must still resolve underneath the
  manifest directory; an output symlink must still resolve underneath
  `--output-dir`.
- Scenario paths, generated compiled sidecars, and final output paths must be unique
  after normalization. A compiled sidecar may not overlap the manifest itself.
- Final MP4 paths and their private `.guidebot_video/<stem>` workspaces must remain
  disjoint after symlink resolution. This also protects composite MP4s and durable
  WAV beds inside those workspaces.
- Unknown manifest fields fail validation.

The explicit output mapping avoids an implicit filename convention and permits safe
subdirectories such as `training/pl-PL/login.mp4`. Language tags still belong in the
recommended names because they make artifacts and upload automation self-describing.

The complete example is
[`examples/localized-login.render-set.yaml`](../../../examples/localized-login.render-set.yaml).

## Variant scenario contract

Each manifest entry points to an ordinary Guidebot scenario. There is no set-specific
step schema. Existing commands retain their normal meaning:

- `say` is narration only;
- `teach` is narration plus the action intent resolved during that scenario's
  compile phase, including a permitted non-sensitive literal typing action;
- `click` and `hover` are explicit localized target descriptions;
- object `wait.until` is a localized target description while `state` and `timeout`
  keep their normal semantics; numeric `wait` is language-independent;
- `enterText.into` is the localized target description. `enterText.text` remains a
  value, may use `${ENV_VAR}`, and is never narration. Secrets still require
  `enterText` rather than a literal inside `teach`;
- `navigate` may be a language-specific absolute URL or a relative URL resolved
  against that scenario's `config.baseUrl`.

Each variant must satisfy these additional v1 invariants:

- `config.locale` equals the manifest language key;
- `config.tts.lang` equals the manifest language key;
- `config.audioTracks` is absent or empty, so the result has exactly one audio
  stream; and
- `config.tts.trackLanguage` is required and is a registered ISO 639-2 code.

The scenario owns its `title`, `baseUrl`, `locale`, TTS voice, narration, action
intents, navigation paths, and input target descriptions. Viewport and cosmetic
settings may also differ because variants are independent films, although matching
viewports are recommended for a coherent published set.

Two complete examples intentionally use different browser locales, base URLs,
navigation paths, narration, and action descriptions:

- [`examples/localized-login.pl-PL.scenario.yaml`](../../../examples/localized-login.pl-PL.scenario.yaml)
- [`examples/localized-login.en-US.scenario.yaml`](../../../examples/localized-login.en-US.scenario.yaml)

### `translations` remains audio-only

`compile-set` never promotes `translations` into action intent. A translated
`teach` inside a multi-audio scenario remains alternate TTS text and the compiler
still sees only the scenario's canonical `teach`.

Render-set variants have no `audioTracks`, so they normally contain no
`translations`. To make an English action resolve against English UI, write a full
English scenario whose canonical `teach`, `click`, `hover`, `wait.until`, or
`enterText.into` is English.

## CLI contract

### Compile

```bash
uv run guidebot compile-set examples/localized-login.render-set.yaml
```

`compile-set` performs these phases:

1. Parse and validate the complete manifest and every referenced scenario before
   launching a browser.
2. Visit variants sequentially in manifest order.
3. For each variant, run the normal compile pipeline and write the normal compiled
   sidecar beside its source scenario.

For the example, the generated sidecars are:

```text
examples/localized-login.pl-PL.compiled.yaml
examples/localized-login.en-US.compiled.yaml
```

`--headed`, `--force`, `--timeout`, `--pause-on-error`, and `--verbose` have the same
meaning as on `compile`. Up-to-date variants can use the existing incremental reuse
rules; compiled actions are never shared across language variants.

### Render

```bash
uv run guidebot render-set \
  examples/localized-login.render-set.yaml \
  --output-dir out/localized-login
```

`--output-dir` is required. It is interpreted relative to the current working
directory and created if necessary. The example publishes:

```text
out/localized-login/localized-login.pl-PL.mp4
out/localized-login/localized-login.en-US.mp4
```

Each output contains exactly one H.264 video stream and one default AAC-LC audio
stream for the variant language. The ordinary work artifacts remain namespaced by
output stem, for example:

```text
out/localized-login/.guidebot_video/localized-login.pl-PL/bed-pol.wav
out/localized-login/.guidebot_video/localized-login.en-US/bed-eng.wav
```

`render-set` is a zero-LLM operation. It must fail with an instruction to run
`compile-set` if any variant's compiled sidecar is missing or stale; it never invokes
the Reasoner automatically. `--headed`, `--timeout`, `--pause-on-error`, and
`--verbose` retain their normal render meanings.

## Isolation and ordering

Every compile or render variant receives a fresh Playwright browser context and page.
The implementation may reuse one browser process for speed, but it must not reuse
cookies, local storage, session storage, service workers, permissions, pages, or a
logged-in session across variants.

This isolation is required even when variants share a host. Locale selection and
language redirects often depend on first-visit cookies; leaking them would make
manifest order change the output.

Each scenario's effective `locale`, `baseUrl`, and `navigate` values are used in both
its compile and render phases. A relative `navigate` is resolved only against that
scenario's `baseUrl`. The manifest does not rewrite URLs.

## Failure and publication semantics

Set execution is sequential and stops on the first error. It is intentionally not a
cross-variant transaction:

- variants completed before the error remain valid and published;
- the failed variant reports its language and underlying compile/render error;
- variants after the failure are not started; and
- the command exits non-zero.

Normal per-scenario atomicity still applies. A failed render must not replace that
variant's previously valid MP4 or WAV bed with a partial artifact. A compile failure
may retain safe incremental work in that scenario's compiled sidecar, but the sidecar
is not considered render-ready until its normal freshness checks pass.

Expanded `${ENV}` values in `enterText.text` or `navigate` are treated as sensitive
diagnostics. If Playwright includes a filled value or tokenized URL in an exception,
Guidebot redacts it from the raised set error, verbose progress, and
pause-for-inspection message.

Rerunning the set resumes through ordinary per-scenario compile reuse. Render-set does
not infer success from an existing MP4; requested variants render again and atomically
replace their own outputs only after success.

## Acceptance gates

- Existing `compile` and `render --out` behavior and YAML remain unchanged.
- The example manifest resolves scenarios relative to itself and outputs under
  `--output-dir`.
- Each full scenario compiles to its own normal sidecar and renders with a fresh
  browser context.
- Each localized MP4 contains exactly one video and exactly one default audio stream
  with the expected language metadata.
- `translations` never affects set compilation.
- Absolute/drive/escaping paths, symlink escapes, sidecar/output/workspace
  collisions, language/config mismatches, and any `audioTracks` in a set variant
  fail validation before browser launch.
- A failure in the second variant preserves a completed first output, does not start
  the third, and never publishes a partial second output.

## Trade-offs

Full scenarios duplicate some structure, but make every browser-affecting value
explicit and reviewable. They avoid a second override language whose merge rules
would have to model every current and future command.

The cost is one compile artifact, browser recording, H.264 encode, and MP4 per
language. Pages can also drift independently. In return, each film has localized UI,
localized action resolution, natural single-language pacing, and the widest player
and upload compatibility.
