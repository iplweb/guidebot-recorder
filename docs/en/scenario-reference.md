# Scenario YAML reference

A source scenario is a YAML mapping with exactly two top-level keys: `config` and
`steps`. Models use a closed schema: unknown keys are errors.

## Complete example

```yaml
config:
  title: "Logging in to the system"
  baseUrl: https://staging.example.com
  viewport:
    width: 1280
    height: 720
  locale: en-US
  tts:
    provider: edge
    voice: en-US-GuyNeural
    lang: en-US
  cursor:
    width: 34
    height: 46
    color: "#ef4444"
    outline: "#ffffff"
    glow: "rgba(239,68,68,.75)"
    easing: "cubic-bezier(.45,.05,.25,1)"
    speed: 1.15
    minDuration: 320
    maxDuration: 1400
    settle: 280
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true

steps:
  - navigate: /login
  - say: "I will show you how to log in."
  - enterText:
      into: "the email address field"
      text: "${DEMO_EMAIL}"
    say: "Enter the email address."
  - enterText:
      into: "the password field"
      text: "${DEMO_PASSWORD}"
    say: "Enter the password."
  - teach: "Click the Log in button"
  - wait: 1.0
  - wait:
      until: "the dashboard heading"
      state: visible
      timeout: 10
```

## `config`

| Field | Required | Type / default | Meaning |
|---|---:|---|---|
| `title` | Yes | string | Human-readable scenario title. |
| `baseUrl` | No | string / none | Base used to resolve relative `navigate` values. |
| `viewport` | Yes | object | Compile/render browser and video dimensions. |
| `locale` | No | string / browser default | Locale used by both compile and render browser contexts and the target fingerprint. |
| `tts` | Yes | object | Narration settings and cache identity. |
| `audioTracks` | No | list / `[]` | Alternate narration streams for the same visual flow. |
| `cursor` | No | object / built-in defaults | Visual cursor appearance and motion. |
| `chrome` | No | object / disabled | Optional browser-bar overlay used only during render. |

### `viewport`

```yaml
viewport: { width: 1280, height: 720 }
```

Both fields are required integers. Use the same viewport when reviewing the target
application: responsive breakpoints change available candidates. The viewport also
sets the recorded video size.

Changing width or height invalidates target fingerprints.

### `baseUrl`

When `baseUrl` is present, a relative navigation is joined to it:

```yaml
baseUrl: https://staging.example.com/app/

steps:
  - navigate: login
```

Absolute `http://` and `https://` values are used unchanged. Environment substitution
does not run in `baseUrl`; put the complete variable in `navigate` if needed. URL
joining follows standard URL semantics: a leading slash, such as `/login`, resets
the base path and would resolve the example above to `https://staging.example.com/login`.

### `locale`

`locale` is optional and participates in the compilation fingerprint. The stock
compiler and renderer both create a fresh Playwright context with this locale and the
configured viewport. An application may still choose language from its URL, account,
or persisted server state; keep those inputs deterministic too.

### `tts`

| Field | Required | Type | Stock CLI behavior |
|---|---:|---|---|
| `provider` | Yes | string | Use `edge`; the stock CLI always constructs the Edge adapter. |
| `voice` | Yes | string | Edge voice name actually used for synthesis. |
| `lang` | Yes | string | Cache/fingerprint language metadata. |
| `model` | No | string | Accepted and included in the cache key; ignored by the Edge adapter. |
| `speed` | No | number | Accepted and included in the cache key; ignored by the Edge adapter. |
| `title` | No | string | MP4 stream title/handler metadata; defaults to `lang`. |
| `trackLanguage` | No* | string | Registered lowercase ISO 639-2 MP4 language code; defaults to `und` for a single track. |

The schema is future-facing, but arbitrary provider names do not select another TTS
adapter. The stock `render` and `render-set` commands reject any configured provider
other than `edge` before recording. Use an Edge-compatible voice. A custom Python
caller may inject another provider implementation.

Changing `tts.lang` invalidates target fingerprints. Changing other TTS fields changes
the audio cache key, except MP4-only `title` and `trackLanguage`.

### `audioTracks`

Alternate tracks use the same fields as `tts`:

```yaml
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
```

When `audioTracks` is nonempty, every default/alternate track requires a unique
`lang` and a unique registered `trackLanguage`. Each narrated step then requires an
exact `translations` map for all alternate `lang` values. Tracks share one locale,
browser flow, and visual timeline; see [Multilingual audio](multilingual-audio.md).

### `cursor`

All cursor fields are optional. They affect rendering only and do not require target
recompilation.

| YAML field | Default | Unit / meaning |
|---|---:|---|
| `width` | `34` | Cursor width in pixels. |
| `height` | `46` | Cursor height in pixels. |
| `color` | `#ef4444` | Arrow fill CSS color. |
| `outline` | `#ffffff` | Arrow outline CSS color. |
| `glow` | `rgba(239,68,68,.75)` | Halo CSS color. |
| `easing` | `cubic-bezier(.45,.05,.25,1)` | CSS movement easing. |
| `speed` | `1.15` | Pixels per millisecond; higher is faster. |
| `minDuration` | `320` | Minimum movement duration in milliseconds. |
| `maxDuration` | `1400` | Maximum movement duration in milliseconds. |
| `settle` | `280` | Pause after arrival and before the action, in milliseconds. |

### `chrome`

The optional macOS-style browser bar is a render-only overlay. It is disabled by
default, and none of its fields participates in the target config hash. Changing it
does not require recompilation.

```yaml
chrome:
  enabled: true
  showUrl: true
  typeOnNavigate: true
```

| YAML field | Default | Meaning |
|---|---:|---|
| `enabled` | `false` | Draw the browser bar. |
| `showUrl` | `true` | Show the address pill. When false, URL typing and its delay are disabled. |
| `typeOnNavigate` | `true` | Type navigation URLs that have no explicit step-level `type` override. |
| `height` | `56` | Bar height in pixels; must be greater than zero. |
| `barColor` | `#f3f4f6` | Bar background CSS color. |
| `textColor` | `#374151` | Address text CSS color. |
| `radius` | `12` | Corner radius in pixels; must be non-negative. |
| `showLock` | `true` | Show a decorative lock for `https:` addresses. |
| `closeColor` | `#ff5f57` | Close-dot CSS color. |
| `minimizeColor` | `#febc2e` | Minimize-dot CSS color. |
| `maximizeColor` | `#28c840` | Maximize-dot CSS color. |

The overlay adds top padding to the document, so it reduces the page's available
layout area inside the same configured video viewport. The displayed address is
synchronized after navigation and when the overlay is ensured, not continuously for
every History API change. Query strings and fragments can appear in the recording;
set `showUrl: false` for secret-bearing URLs. The overlay is installed on the initial
`about:blank` page; put the first `navigate` before introductory narration if you do
not want that blank address visible during the intro.

Unknown fields are rejected. `height` must be positive and `radius` non-negative;
color strings are passed through as CSS values without validating CSS syntax.

## `steps`

`steps` is an ordered list. A step may contain:

- exactly zero or one **main command** from `teach`, `navigate`, `click`, `hover`,
  `enterText`, and `wait`;
- an optional `say` narration;
- an optional `translations` mapping for configured alternate audio tracks;
- at least `say` when there is no main command.

Two main commands in one item are invalid. Split them so compilation can reproduce
page state and align one generated action slot with each source step.

| Command | Agent target resolution? | Narration by default? |
|---|---:|---:|
| `say` | No | Its own text |
| `navigate` | No | No |
| `teach` | Yes | Its own text |
| `click` | Yes | No |
| `hover` | Yes | No |
| `enterText` | Yes, `into` only | Only accompanying `say` |
| numeric `wait` | No | Only accompanying `say` |
| conditional `wait` | Yes, `until` | Only accompanying `say` |

If `say` accompanies an action, narration is rendered before the action. With
multiple audio tracks, all translations start together and the longest one controls
when the shared action occurs.

### `translations`

```yaml
- click: "the Save button"
  say: "Save the changes."
  translations:
    pl-PL: "Zapisz zmiany."
```

Keys must match `audioTracks[*].lang` exactly. Every narrated step requires every
alternate key; the default `tts.lang`, unknown keys, missing keys, or translations on
a non-narrated step are validation errors. If `say` accompanies `teach` or another
action, translations correspond to `say`. They never change the canonical browser
action and are not environment-substituted.

### `say`

Pure narration:

```yaml
- say: "Welcome to the account settings tutorial."
```

Or narration attached to one main command:

```yaml
- click: "the Settings link"
  say: "Open Settings."
```

`say` is not environment-substituted. Never put a secret in it.

### `navigate`

```yaml
- navigate: /login
- navigate: https://other.example.com/start
- navigate: "${APP_URL}/login"
- navigate: { url: /reports, type: false }
```

This calls Playwright directly; no agent is involved. A relative value uses
`baseUrl`. Environment substitution is supported in both the string form and the
object's `url`. With browser chrome enabled, object-form `type` overrides
`chrome.typeOnNavigate` for that step: `true` types the address before loading and
`false` loads first and updates the pill instantly. It affects render only; compile
always navigates directly.

### `teach`

```yaml
- teach: "Click the Log in button"
- teach: "Hover over the Help menu"
- teach: "Type demo@example.com into the e-mail field"
```

`teach` is spoken as narration and lets the reasoner infer the operation. Compiler v2
supports click, hover, and a safe literal type demonstration. For type, `inputText`
must be a nonempty exact substring of the trusted instruction and is frozen in the
sidecar. Guidebot rejects sensitive wording, ENV placeholders, invented text, and
password-like targets. Use explicit `click`/`hover` when the action must be fixed and
`enterText` with `${ENV_VAR}` for sensitive or replaceable typing.

Keep `teach` to one executable operation and one semantic target. Split “click A and
then click B” into two steps. It is not environment-substituted.

### `click` and `hover`

```yaml
- click: "the Save changes button"
  say: "Save the new settings."

- hover: "the Reports menu"
```

These commands fix the action kind while the agent resolves the target description.
They are useful when action text and narration should differ.

### `enterText`

```yaml
- enterText:
    into: "the email address field"
    text: "${DEMO_EMAIL}"
  say: "Enter the account email address."
```

`into` is a semantic target instruction sent to the reasoner. `text` is filled by
Playwright and is never sent in the candidate prompt. Environment substitution runs
only in `text`, not `into`.

Playwright uses `fill`, which replaces the current value. A normal text field can
show the value in the recording; Guidebot does not add masking.

### Numeric `wait`

```yaml
- wait: 1.5
```

Pauses for seconds in both compile and render without an agent. Use non-negative
values. This is sometimes necessary before resolving UI that appears asynchronously.

### Conditional `wait`

```yaml
- wait:
    until: "the results table"
    state: visible
    timeout: 10
```

Fields:

| Field | Required | Default | Values |
|---|---:|---:|---|
| `until` | Yes | — | Semantic element description. |
| `state` | No | `visible` | `visible`, `hidden`, `enabled`. |
| `timeout` | No | `10.0` | Seconds. |

The target is agent-resolved during compile. A hidden wait intentionally stores no
identity because there may be no matching element after success.

!!! warning "Beta wait limitations"

    Compile-time target validation generally needs the element to be present and
    visible before the conditional wait starts. If an element appears only after a
    delay, add a numeric wait first. Also, the current `enabled` implementation waits
    for visibility rather than independently polling the enabled predicate. Do not
    rely on it as a strict enabled-state gate yet.

## Pop-up behavior

Pop-ups require no source command. If a compiled click (including `teach` inferred as
click) opens one new Playwright page, compiler v2 stores `opens_popup: true`, makes it
active for following steps, and returns to the main page when a scenario action
closes it. Render reproduces and visually composes the same lifecycle. A pop-up left
open remains visible through the video end.

Only one pop-up lifecycle is supported. A second, simultaneous, unexpected, or
independently closing page fails. There is no explicit tab/window switch command, and
targets inside any iframe remain unsupported.

### `expect` is not a supported authoring control

The internal step model currently accepts an `expect` field, but the compiler derives
readiness from observed URL change and does not honor the source value as a stable
user control. Do not add `expect` to authored scenarios. For same-URL SPA updates,
use explicit waits and verify the result.

## Environment substitution

`${NAME}` is expanded only in:

- string-form `navigate` or object-form `navigate.url`;
- `enterText.text`.

Substitution does not run in `baseUrl`, `say`, `teach`, `translations`, target
instructions, or any TTS/config field.

A variable may appear more than once. Missing variables raise an error. `$${` escapes
a literal `${` sequence:

```yaml
- enterText: { into: "the template field", text: "$${USER}" }
```

This fills the literal text `${USER}`.

The generated sidecar uses compiler schema version 2 and must be regenerated rather
than edited. See [Scenario files](scenario-files.md#the-generated-sidecar) for its
layout and lifecycle.
