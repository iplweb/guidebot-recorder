# Troubleshooting

Start diagnostic runs with a visible browser and detailed progress:

```bash
uv run guidebot compile path/to/flow.scenario.yaml \
  --headed --pause-on-error --verbose
```

## Codex CLI is missing

Symptom: compilation reports that Codex CLI is required or cannot find `codex`.

```bash
npm install -g @openai/codex
codex --version
codex login
codex login status
```

Guidebot launches the local `codex` executable. An IDE extension or desktop app alone
does not guarantee that this executable is on `PATH`.

## Codex authentication fails

Run `codex login` interactively, then confirm `codex login status`. Codex CLI supports
ChatGPT sign-in and API-key authentication. Guidebot has no separate authentication
configuration and reuses the Codex session.

If ordinary `codex exec` cannot run under that account or workspace, Guidebot cannot
use it either.

## Compile says “nothing to compile” after the site changed

The browser-free fast path validates compiler-v2 provenance, action alignment,
command kinds, target instructions, wait states, and relevant config hash. It cannot
see a target-application change or prove that an edited `navigate`, `baseUrl`, or
account state still leads to the same page. Force a live rebuild:

```bash
uv run guidebot compile path/to/flow.scenario.yaml --force --headed -v
```

Use the same command after a Guidebot upgrade when you want to refresh every frozen
target.

A normal first compile creates an all-`null` sidecar for a nonempty scenario with no
element-targeted steps. A scenario with `steps: []` still produces no sidecar even
with `--force` and cannot be rendered.

## Render reports a missing action or identity mismatch

The source and sidecar are missing, misaligned, or stale. Do not patch the sidecar.

```bash
uv run guidebot validate path/to/flow.scenario.yaml
uv run guidebot compile path/to/flow.scenario.yaml --force --headed -v
```

Review the regenerated `*.compiled.yaml`, then render again.

Render now fails before TTS/browser use when source name, compiler version, slot
count, action kind, fingerprint, wait state, or relevant configuration is stale.
During replay it also validates live identity for click, hover, and type. Conditional
waits skip only that live identity comparison. Route-only or application-only drift
can still pass static preflight, so use `compile --force` for those changes.

## The reasoner cannot find an element

Try, in order:

1. Match the configured viewport to the layout you expect.
2. Make the instruction semantic: use visible name, role, label, section, or purpose.
3. Split instructions so there is exactly one action and one target per step.
4. Add a numeric wait before content that appears asynchronously.
5. Use `--headed --pause-on-error -v` and inspect the actual page state.
6. Confirm that the element is not inside an iframe. A supported pop-up must have
   been opened by the preceding compiled click and be the only pop-up lifecycle.

The candidate snapshot is capped at 200 current semantic elements and normally
contains only visible elements intersecting the configured viewport. There is no
source `scroll` command. Guidebot can scroll to a target after it has a locator, but
the agent may not be able to resolve an element that was absent from its snapshot.

## A localized page differs between compile and render

Stock compile and render both create fresh contexts with `config.locale`. If the page
still differs, its language is probably controlled by a URL, application setting,
account preference, or server state. Make that input deterministic. If the localized
page also changes routes or target labels, use a
[localized render set](localized-render-sets.md).

## `teach` cannot type a value

Compiler v2 accepts literal demonstrations such as “Type `demo@example.com` into the
e-mail field”. The reasoner must return that exact nonempty substring as `inputText`.
Guidebot rejects invented text, `${ENV_VAR}` placeholders, sensitive wording, and
password-like target fields, retrying resolution before failing.

For passwords, tokens, credentials, passcodes, API keys, or replaceable values, use:

```yaml
- enterText: { into: "the password field", text: "${DEMO_PASSWORD}" }
```

## A `${VARIABLE}` is not expanded

Substitution works only in string `navigate`, object `navigate.url`, and
`enterText.text`. It does not work in `baseUrl`, narration, target instructions, or
other config fields.

Export the value before `validate`, `compile`, and `render`:

```bash
export DEMO_EMAIL=user@example.com
```

A repository `.env` file is ignored by Git but is not loaded by Guidebot. `$${` is
the escape for a literal `${`.

## TTS or ffprobe fails

Confirm that both programs are available:

```bash
ffmpeg -version
ffprobe -version
```

The stock renderer uses Edge TTS and therefore needs network access when a narration
segment is not already in `.guidebot/audio/`. Use an Edge voice name in `tts.voice`.
Stock `render`/`render-set` reject a provider other than `edge` before recording; all
tracks/variants must use one provider. `model` and `speed` enter the cache key but the
current Edge adapter uses only `voice` for synthesis.

To regenerate narration, remove only the relevant cache entries or use a different
voice/text setting. The cache key changes automatically with TTS config and text.
The cache persists and its JSON metadata contains the narration text; delete it
manually when it should no longer remain on disk.

## Multilingual validation or muxing fails

Check [Multilingual audio](multilingual-audio.md), especially:

- unique `lang` on the default and every alternate track;
- unique registered lowercase ISO 639-2 `trackLanguage` on every track;
- exactly one `translations` entry per alternate `lang` on every narrated step;
- no translations on a step without `say`/`teach`;
- `provider: edge` on every track for the stock CLI.

Guidebot synthesizes all languages before recording and waits for the longest
narration at each step. Durable beds are under
`<output-dir>/.guidebot_video/<stem>/bed-<trackLanguage>.wav`. If a bed or mux fails,
the previous MP4 and complete bed set are preserved.

## A localized render set fails before Chromium

Both set commands preflight the whole manifest. Verify that each variant key is a
canonical BCP 47 tag and equals the scenario's `config.locale` and `config.tts.lang`;
`trackLanguage` is present; `audioTracks` is empty; all providers match; and all
scenario/output paths are unique portable relative paths. `render-set` additionally
requires every compiler-v2 sidecar to be current and all outputs to stay safely below
`--output-dir`. Run `compile-set` first. See
[Localized render sets](localized-render-sets.md).

## A same-URL SPA transition races

Compile derives navigation readiness from URL change. A SPA may update without
changing the URL. Add an explicit wait after the triggering action. Because a
conditional target must normally be resolvable during compile, precede it with a
short numeric pause when the element appears later:

```yaml
- teach: "Click the Search button"
- wait: 1.0
- wait: { until: "the results table", state: visible, timeout: 10 }
```

## Conditional `wait` does not behave as expected

Current beta constraints:

- compile-time resolution generally requires a present, visible target;
- `hidden` may succeed with no element and intentionally has no frozen identity;
- `enabled` currently waits for visibility rather than separately polling whether the
  element has become enabled.

Do not rely on `enabled` as a strict gate yet.

For an element that may not appear at all — a cookie banner, a promo interstitial —
reach for an optional branch (`when`) rather than a numeric wait. It polls for the
element and skips its steps when the element never shows, instead of failing the run.
See [Optional branches](scenario-reference.md#optional-branches).

## The synthetic browser bar changes the layout or URL

`config.chrome` injects a DOM overlay during render; it is not real Chromium UI.
The window dots are decorative and the overlay uses `pointer-events: none`.

The bar reserves `chrome.height` pixels by increasing the top padding of `<html>`
inside the existing viewport. The MP4 dimensions do not grow. Sticky/fixed UI or a
responsive breakpoint can therefore differ from compile, which never injects the
bar, even with `--headed`. Adjust the viewport or disable the bar if this changes the
flow.

The address is synchronized on navigation and the next overlay `ensure`, not on every
History API or hash update. It can show `about:blank` during narration before the
first navigation. Put `navigate` first when needed. The full URL, including query and
fragment, can enter the video; set `showUrl: false` for secret-bearing addresses.
The lock icon is only decorative and appears for `https:` URLs — it is not a security
verification.

## A pop-up or iframe flow stops

Guidebot follows one pop-up lifecycle automatically when a compiled click opens
exactly one new page. Subsequent steps operate there; after a scenario action closes
it, control and the final video return to the main page. If left open, the pop-up
remains visible through the end.

It fails loudly for a second/sequential or simultaneous pop-up, a page opened outside
the actual click window, a non-click opener, or a pop-up that closes asynchronously
during narration. There is no explicit switch-page command. Iframe traversal of any
origin remains unsupported.

## `--auto-heal` exits immediately

The flag is reserved and deliberately returns “not implemented.” Repair is explicit:
run `compile --force`, review the changed sidecar, then restart render from step zero.
No agent is allowed to alter targets during recording.

## Current limitations

- Codex CLI is the only built-in compilation backend.
- There is no `--reasoner` or `--model` selection.
- The built-in Codex call cannot use subagents, skills, plugins, MCP, browser tools,
  computer use, shell tools, or web search.
- Chromium is the only browser launched by the stock CLI.
- Each command starts a fresh session; there is no storage-state/cookie import option.
- Exactly one automatically followed pop-up lifecycle is supported; arbitrary tabs,
  a second pop-up, explicit switching, and all iframe content are unsupported.
- There is no route discovery, manual recording, or scenario-generation command.
- `--auto-heal` is not implemented.
- Edge TTS is the only stock narration adapter.
- Secret substitution does not mask browser values in video or application logs.
- `.guidebot/audio/` and `.guidebot_video/` persist until manually removed and may
  contain narration text, audio, or recorded page data.
- Candidate collection is viewport-oriented and capped at 200 elements.
- Alternate audio shares one visual locale and route; use a render set for localized
  pages.

Treat Guidebot as a deterministic renderer for reviewed, resettable flows — not as an
autonomous web exploration agent.

## The documentation build fails

Install the documentation group and run the strict build:

```bash
uv sync --group docs
uv run --group docs mkdocs build --strict
```

English and Polish have intentionally separate files with fallback disabled. A
missing translation, broken link, or invalid navigation entry should be fixed rather
than silently falling back to English.
