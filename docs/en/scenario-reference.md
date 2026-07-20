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
    bow: 0.12
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
| `setup` | No | string / none | On a **target**: path to a setup scenario whose cached session is established before compile and render. Participates in the target's compile hash. |
| `verifyUserLoggedIn` | No | string or object / none | On a **setup**: login health-check for the cached session. Render-only (outside the setup's compile hash). |
| `maxAgeHours` | No | number / none | On a **setup**: optional TTL for the cached session. Render-only (outside the setup's compile hash). |
| `tts` | Yes | object | Narration settings and cache identity. |
| `audioTracks` | No | list / `[]` | Alternate narration streams for the same visual flow. |
| `cursor` | No | object / built-in defaults | Visual cursor appearance and motion. |
| `chrome` | No | object / disabled | Optional browser-bar shell used only during render. |
| `popup` | No | object / built-in defaults | How a pop-up window is composited into the film (render-only). |
| `typing` | No | object / instant fill | Character-by-character input animation, render-only. |
| `sound` | No | object / disabled | Opt-in built-in click/key sound effects, render-only. |
| `intro` | No | object / disabled | Opt-in intro title card shown before step 1, render-only. |
| `holdFrameForNarration` | No | boolean / `true` | Freeze the picture during narration instead of recording in real time, render-only. |
| `holdFrameSettle` | No | number / `1.0` | Real seconds recorded before the frame freezes, render-only. |

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

### `setup`, `verifyUserLoggedIn`, and `maxAgeHours`

These three keys implement **pre-recording setup**: recording a target scenario
with the site already prepared (logged in, cookies accepted) without that
preparation appearing on the film. The preparation runs on a separate,
non-recording browser context, and the resulting session (a Playwright
`storage_state`) is cached and reused.

**`setup` — on the target scenario.** A path, relative to the target file, to a
setup scenario — an ordinary, already-compiled `*.scenario.yaml`, typically one
that teaches logging in. The target then omits its own login steps.

```yaml
config:
  setup: teach-login.scenario.yaml
```

`guidebot compile` and `guidebot render` of the target both auto-establish or
reuse the cached session before their own work when `setup` is present. The
**setup scenario must be compiled first** (`guidebot compile teach-login.scenario.yaml`);
otherwise compile, render, or `guidebot setup` of the target fail loudly and tell
you to compile the setup. Establishing the session replays the setup's frozen
targets and makes **zero LLM calls**. A scenario used as a setup source must not
itself declare `config.setup` (recursion is a validation error), and the setup
and target must share the same origin (host); cross-origin reuse is a hard error.

Because `setup` changes the authenticated DOM that compile resolves against, it
is **not cosmetic**: it participates in the target's compile hash (see the
[recompile matrix](#recompile-matrix)), so adding, removing, or changing it — or
changing the login user — re-resolves the target.

**`verifyUserLoggedIn` — on the setup scenario.** The login health-check that
decides whether a cached session is still valid. It accepts a string (shorthand
for `containsText`) or an object:

```yaml
config:
  verifyUserLoggedIn: "Sign out"     # shorthand for {containsText: "Sign out"}
  # full form:
  # verifyUserLoggedIn:
  #   containsText: "Sign out"       # required
  #   url: /dashboard                # optional; defaults to the target's baseUrl
  #   timeout: 8                     # optional; seconds, default 8
```

| Field | Required | Default | Meaning |
|---|---:|---:|---|
| `containsText` | Yes | — | Text that must be present on the page for the session to count as live. |
| `url` | No | target `baseUrl` | Page to visit before checking. Cookies are origin-scoped, so the check runs against the target's origin. |
| `timeout` | No | `8` | Seconds to poll for `containsText` before deciding the session is logged out. |

The match is a plain, **case-sensitive substring** of the page's rendered
`document.body.innerText`. Choose text that renders **only when authenticated** —
a username is the robust choice; because the match has no word boundaries, a
logged-out footer like `sign out whenever you like` would false-positive.

**`maxAgeHours` — on the setup scenario.** Optional time-to-live for the cached
session, computed from the cache's `created_at` (not file mtime, so it survives
`git clean`, copies, and CI restore). When the age is exceeded, the session is
refreshed on the next compile/render/`setup`.

If a setup scenario declares **neither** `verifyUserLoggedIn` **nor**
`maxAgeHours`, a present cache is trusted until `--force`, and the tool prints a
loud warning. The "never silently logged-out" guarantee holds only when a
health-check is configured.

Both `verifyUserLoggedIn` and `maxAgeHours` are **render-only** on the setup
file: they stay outside the setup file's own compile hash. See
[`guidebot setup`](cli-reference.md#guidebot-setup) for building or refreshing the
cache by hand.

!!! warning "Known limitations (v1)"

    Only cookies and `localStorage` are cached; a session kept in
    `sessionStorage` or IndexedDB (some OIDC/MSAL SPAs) cannot be cached, and the
    tool detects and reports it. Setup and target must share the same origin. One
    language-agnostic session is reused across localized render-set variants; if a
    backend pins UI language to the session, frozen localized labels can mismatch.

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
recompilation. The cursor now starts at the centre of the viewport on every render
(previously the top-left corner); this is a fixed cosmetic change with no config
knob.

| YAML field | Default | Unit / meaning |
|---|---:|---|
| `width` | `34` | Cursor arrow width in pixels. |
| `height` | `46` | Cursor arrow height in pixels. |
| `color` | `#ef4444` | Arrow fill CSS color. |
| `outline` | `#ffffff` | Arrow outline CSS color. |
| `glow` | `rgba(239,68,68,.75)` | Halo CSS color. |
| `easing` | `cubic-bezier(.45,.05,.25,1)` | Movement easing (a `cubic-bezier(...)` curve, evaluated in JS). |
| `bow` | `0.12` | Depth of the arc the pointer traces, as a fraction of travel distance. `0` gives straight-line moves. |
| `speed` | `1.15` | Pixels per millisecond; higher is faster. |
| `minDuration` | `320` | Minimum movement duration in milliseconds. |
| `maxDuration` | `1400` | Maximum movement duration in milliseconds. |
| `settle` | `280` | Pause after arrival and before the action, in milliseconds. |
| `click` | built-in defaults | Click ripple appearance; see `cursor.click` below. |

For a bigger, easier-to-follow pointer at higher-resolution viewports, scale `width`
and `height` up together, e.g. `46`/`62`.

#### `cursor.click`

The click ripple's appearance. Defaults reproduce today's ripple exactly, so
omitting `click` entirely keeps the existing look.

| YAML field | Default | Meaning |
|---|---:|---|
| `color` | `rgba(37,99,235,.9)` | Ripple ring CSS color. |
| `scale` | `3.25` | Ring end-scale; must be greater than `0`. |
| `flash` | `false` | When `true`, adds a brief filled disc under the ring for a stronger click flash. |

### `chrome`

The optional macOS-style browser bar is an **iframe shell** rendered only during
render. It is disabled by default. The target site renders inside an `<iframe>`
mounted **below** the bar, so the bar can never obscure page content — this is a
structural guarantee, not top padding. With chrome enabled, the site's layout
viewport becomes `width × (height − chrome.height)`.

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
| `height` | `56` | Bar height in pixels; must be greater than zero. Shrinks the site viewport. |
| `barColor` | `#f3f4f6` | Bar background CSS color. |
| `textColor` | `#374151` | Address text CSS color. |
| `radius` | `12` | Corner radius in pixels; must be non-negative. |
| `showLock` | `true` | Show a decorative lock for `https:` addresses. |
| `closeColor` | `#ff5f57` | Close-dot CSS color. |
| `minimizeColor` | `#febc2e` | Minimize-dot CSS color. |
| `maximizeColor` | `#28c840` | Maximize-dot CSS color. |
| `interactOnNavigate` | `true` | On a navigate step the cursor glides to the address pill, clicks, the pill takes a focused look, then the URL is typed. |
| `charDelayMs` | `60` | Base per-character typing delay in milliseconds. |
| `charJitterMs` | `55` | Jitter band (ms) around the per-character delay. The draw is right-skewed (log-normal): most characters land near `charDelayMs`, a minority are noticeably slower, and none is faster than `charDelayMs − charJitterMs`. |
| `segmentPauseMs` | `180` | Pause between URL segments, in milliseconds. It fires only at a *real* boundary — a doubled separator such as the second `/` in `://` is typed as one motor burst and gets no pause. |
| `maxDelayFactor` | `2.5` | Hard ceiling on a single character's delay, as a multiple of `charDelayMs`. The occasional deliberate "thinking" beat never stacks on a segment pause, so no keystroke stalls absurdly. |
| `preNavigatePauseMs` | `400` | Pause after typing completes and before the load, in milliseconds. |
| `focusColor` | `#3b82f6` | Focused-pill accent CSS color. |
| `showCaret` | `true` | Show a blinking caret in the focused pill while typing. |

Most chrome fields are cosmetic and stay **outside** the config hash, so tweaking
them never forces a recompile. The two exceptions are `enabled` and `height`: both
change the compiled site viewport (the iframe is `height − chrome.height` tall), so
they **do** participate in the config hash — toggling chrome on or off, or changing
its height, forces a recompile. The typing and interaction fields
(`interactOnNavigate`, `charDelayMs`, `charJitterMs`, `segmentPauseMs`,
`maxDelayFactor`, `preNavigatePauseMs`, `focusColor`, `showCaret`) are render-only
visuals and stay
outside the hash.

To load arbitrary sites inside the iframe, the render step strips the
`X-Frame-Options` header and the CSP `frame-ancestors` directive from responses and
blocks service workers during render. A site that redirects on its entry URL loads
at that entry URL, and the pill shows the navigated URL, not the post-redirect one.
The displayed address is synchronized after navigation and when the shell is ensured,
not continuously for every History API change. Query strings and fragments can appear
in the recording; set `showUrl: false` for secret-bearing URLs. The shell is
installed on the initial `about:blank` page; put the first `navigate` before
introductory narration if you do not want that blank address visible during the intro.

Unknown fields are rejected. `height` must be positive and `radius` non-negative;
color strings are passed through as CSS values without validating CSS syntax.

### `typing`

Render-only character-by-character input animation, **on by default**. Form fields
are typed with the same natural feel as the address bar — a base per-character delay
plus jitter. Compilation always uses instant fill; only `render` animates typing.

| YAML field | Default | Meaning |
|---|---:|---|
| `animate` | `true` | Type each character in the render instead of pasting the value instantly. Set `false` per scenario to keep the instant fill. |
| `speed` | `60` | Base milliseconds **per character** — a delay; higher is slower. Unrelated to `cursor.speed`, which is a pixels-per-millisecond rate; the two are not interchangeable. |
| `jitterMs` | `40` | Jitter band (ms) around `speed`, so typing is natural, not metronomic. Right-skewed like the address bar: mostly near `speed`, occasionally slower, never below `speed − jitterMs`; a doubled character keeps only a fifth of the band. |
| `maxDelayFactor` | `2.5` | Hard ceiling on a single character's delay, as a multiple of `speed`. |

Set `animate: false` for masked, formatted, or autocomplete-driven fields, where a
character-by-character render could misrepresent the final value (the final value is
corrected regardless).

### `sound`

Render-only built-in sound effects mixed under the narration on every language
track, **on by default**. Sounds are bundled with Guidebot; there is no
author-supplied file.

| YAML field | Default | Meaning |
|---|---:|---|
| `enabled` | `true` | The sound effects bed. Set `false` for a silent film (narration only). |
| `click` | `true` | Play a soft click sound on each click (and the address-bar pill click). |
| `keys` | `true` | Play a subtle key-tick per typed character — both in form fields (when `typing.animate`) and while the **address bar** is typed. |
| `volume` | `-12.0` | dB attenuation applied to the effects bed; must be `0` or lower. |

### `intro`

Render-only, opt-in intro title card. When enabled, it opens the film in place of
today's blank white first frame; when disabled (the default), the render keeps the
identical white bootstrap.

| YAML field | Default | Meaning |
|---|---:|---|
| `enabled` | `false` | Show the intro title card. |
| `subtitle` | none | Optional subtitle text. |
| `notes` | none | Optional additional notes text. |

The card is built from `config.title` plus `intro.subtitle` and `intro.notes`.

### `holdFrameForNarration` and `holdFrameSettle`

Render-only pacing control, **on by default**, and outside the config hash like
`cursor` and `popup`.

| YAML field | Default | Meaning |
|---|---:|---|
| `holdFrameForNarration` | `true` | Instead of keeping the browser running for the whole length of a step's narration, record only a `holdFrameSettle`-second sample and freeze that picture; an ffmpeg pass afterwards holds the frame for the rest of the voice-over. |
| `holdFrameSettle` | `1.0` | Seconds of real time still recorded before the frame is held, giving an animation the step triggers — an accordion opening, content fading in — time to finish under the voice, exactly as before this feature existed. The settle is paid *out of* the narration, not on top of it, so the finished film's length is unchanged. If a step's narration is shorter than `holdFrameSettle`, the whole step still records in real time and no freeze happens. Must be at least `2/25` s (two frames at the renderer's 25 fps): below one frame the settle is not representable on the render's frame grid at all. The second frame is a deliberate margin above that one-frame minimum, not something the representability argument alone requires — `1/25` s has been verified to render correctly. |

The finished film has the **same length and pacing** whether `holdFrameForNarration`
is on or off — only recording time changes. But it can **look different**: with the
default on, the page sits still under the voice-over wherever it used to keep
animating. Re-rendering an existing scenario with this default will not reproduce
the pixels of a film rendered before this feature — only its length and timing. Use
`guidebot render --no-hold-frame` to record fully live, as before; see the
[CLI reference](cli-reference.md).

### `popup`

The optional `popup` object controls how a pop-up window (see
[Pop-up behavior](#pop-up-behavior)) is composited into the film. It is render-only:
like `cursor`, **none** of its fields participates in the config hash, so changing it
never requires a recompile.

```yaml
popup:
  transition: slide
  slideMs: 400
```

`transition` selects how the pop-up appears:

- `cut` — a hard cut to the full-frame pop-up recording (the original behavior).
- `float` — the pop-up is a rounded floating window with a drop shadow over the
  **dimmed** main page, which stays visible behind it; it fades in and out.
- `slide` — the pop-up slides in as a **full-frame** window (push-left: the main
  page exits left, the pop-up enters from the right), holds full-frame while active,
  then slides out on close.

| YAML field | Default | Meaning |
|---|---:|---|
| `transition` | derived from `floating` | `cut`, `float`, or `slide` (see above). |
| `floating` | `true` | Deprecated bool alias: `true` → `float`, `false` → `cut`. An explicit `transition` wins. |
| `scale` | `0.85` | `float`: floating window size as a fraction of the viewport. |
| `cornerRadius` | `14` | `float`: window corner radius in pixels. |
| `shadow` | `true` | `float`: draw the drop shadow. |
| `backdropDim` | `0.45` | `float`: opacity of the dark backdrop over the main page. |
| `backdropBlur` | `0` | `float`: backdrop blur radius in pixels. |
| `openMs` | `320` | `float`: fade-in duration in milliseconds. |
| `closeMs` | `240` | `float`: fade-out duration in milliseconds. |
| `slideMs` | `400` | `slide`: slide-in/slide-out duration in milliseconds. |

Composited pop-ups (`float` and `slide`) render **bare**: the pop-up window itself
has no address bar — only the compositor frame is drawn.

!!! note "Known limitation"

    The pop-up uses the size its `window.open(...)` call requested. If that is
    smaller than the video viewport, the framed or full-frame window shows empty
    space around the pop-up content. Forcing the pop-up to fill the viewport is a
    planned improvement.

## `steps`

`steps` is an ordered list. A step may contain:

- exactly zero or one **main command** from `teach`, `navigate`, `click`, `hover`,
  `enterText`, `wait`, and `slide`;
- an optional `say` narration;
- an optional `translations` mapping for configured alternate audio tracks;
- an optional `optional: true` marker (see [Optional branches](#optional-branches));
- at least `say` when there is no main command.

A list entry may also be a `when` block instead of a step; see
[Optional branches](#optional-branches).

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
| `slide` | No | Only accompanying `say`; on-screen text is shown, not spoken |

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
    delay, add a numeric wait first — unless the element is genuinely conditional, in
    which case use an [optional branch](#optional-branches) instead: a `when` gate
    polls for its element and tolerates its absence, so no priming wait is needed.
    Also, the current `enabled` implementation waits for visibility rather than
    independently polling the enabled predicate. Do not rely on it as a strict
    enabled-state gate yet.

### `slide`

```yaml
- slide:
    title: "Logging in to the system"
    subtitle: "Step by step"       # optional
    notes: "Training material"     # optional
    hold: 2.5                      # optional; seconds to hold when there is no `say`
  say: "Let's get started."        # optional narration, SEPARATE from the on-screen text
```

A full-frame text card shown anywhere in the flow, without disturbing the underlying
page. At least one of `title`, `subtitle`, or `notes` is required.

The on-screen slide text is **shown, not spoken**; narration comes only from the
accompanying `say`. In a multilingual video the on-screen text stays single-language
(one shared picture) — only `say` (and its `translations`) switches across
`audioTracks`.

Pacing: with a `say`, the card is paced by the narration and `hold` is ignored;
without a `say`, it holds for `hold` seconds (default `2.5`). A narrated slide
therefore cannot linger after its narration ends. To hold a card *after* speech,
follow it with a second, silent `slide` (same text, a `hold`, no `say`).

Adding, removing, or reordering a `slide` step changes the step count, so it is the
one step kind that **needs `guidebot compile`**; render preflights the step count
against the sidecar and fails loudly otherwise.

## Optional branches

Some parts of a flow are genuinely conditional. The canonical case is a cookie consent
banner: it shows on one run and not on the next, depending on stored consent, A/B
bucketing, or geography. Without an explicit marker such a step is a hard failure — the
wait times out and the whole run dies.

An **optional branch** marks a group of steps as "run these only if this element is
there". If the element is absent, the branch is skipped — including its narration, which
is removed from the timeline rather than left as silence — and the following steps run
normally.

### The `when` block

A `when` block is a top-level entry in `steps`, alongside ordinary steps:

```yaml
steps:
  - navigate: https://www.example.com

  - when: "the cookie consent banner"
    state: visible
    timeout: 20
    steps:
      - teach: "click the button that continues to the site"
      - say: "We accept the cookies and move on."

  - teach: "click the account icon"      # always runs
```

| Field | Required | Default | Meaning |
|---|---:|---:|---|
| `when` | Yes | — | Semantic description of the gating element. |
| `state` | No | `visible` | `visible`, `hidden`, `enabled` — as in a conditional `wait`. |
| `timeout` | No | `10.0` | Seconds to wait for the gating element before deciding it is absent. |
| `steps` | Yes | — | The steps to run when the gate is satisfied. |

The gate behaves like a conditional `wait` whose timeout is not an error. Give the
banner enough `timeout` to appear; a gate that is merely slow and a gate that is
genuinely absent are indistinguishable from the outside.

`when` blocks **cannot be nested**. A `when` inside another block's `steps` is a
validation error. There is no `else` and no alternative branch.

### `optional: true` on a single step

For a single conditional step, add `optional: true` instead of wrapping it in a block:

```yaml
- click: "the 'dismiss' link in the notification bar"
  optional: true
```

It is allowed on steps that resolve a target — `teach`, `click`, `hover`,
`enterText`, and conditional `wait` — and on a numeric `wait`. It is a **validation
error** on `say`-only, `navigate`, and `slide` steps: those resolve nothing, so
"optional" would promise a tolerance Guidebot cannot provide.

### Compile and render

`guidebot compile` does not fail when the gating element is missing. It records the gate
and every child of the branch as *pending* in the sidecar, prints a warning, and exits
`0`. A pending entry counts as up to date, so a later `compile` does not relaunch the
browser only to burn the gate timeout again; use `--force` to retry resolution.

`guidebot render` handles a pending branch **in place**: if the gate does appear, the
renderer calls the reasoner, polls until the element resolves or `timeout` elapses,
executes the children, and rewrites `.compiled.yaml` so that every later render of that
branch is deterministic and LLM-free. If the reasoner is unavailable, render warns loudly
and skips the branch rather than failing.

### Error boundary

Optional does not mean "ignore errors". Only these signals count as *element absent*:

| Situation | Counts as absent |
|---|---|
| Gate with a compiled action | Playwright `TimeoutError` from the wait |
| Gate still pending | The poll window elapses, or the reasoner answers `no_action` / `no_handle` |
| Optional step, still pending | The reasoner answers `no_action` / `no_handle` |
| Optional step with a compiled action | The cached target no longer validates for reuse |

Everything else still fails the render. In particular **`multiple_actions` — an ambiguous
target description — is a hard error**, inside an optional branch as much as outside it.
An ambiguous description is an authoring mistake, not a missing element; swallowing it
would let a typo silently delete a branch from the video.

Errors *inside* a branch that has started are likewise fatal: a click failing on an
already-resolved target, a navigation error, or a wait that times out on a non-gate step
all fail the render as usual.

!!! warning "Known limitations"

    **A self-healing render freezes a frame.** Render records wall-clock time, so the
    in-place reasoner call for a pending branch — up to two minutes — freezes a frame in
    the output video, and an absent branch costs its `timeout` in dead air. Treat the
    render that first resolves a branch as a throwaway; re-render afterwards for a clean
    film. Cutting these stalls out of the timeline is planned.

    **Pop-ups inside an optional branch are unsupported.** A click resolved at render
    time carries no `opens_popup` observation from compile, so a pop-up opened from
    inside a branch fails the render with "unexpected popup". Keep pop-up-opening clicks
    outside optional branches.

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

## Recompile matrix

| Change | Needs `guidebot compile`? |
|---|---:|
| `cursor` (size, `click`, centred start) | No — render-only |
| `typing`, `sound`, `intro`, `chrome` | No — render-only |
| `holdFrameForNarration`, `holdFrameSettle` | No — render-only |
| `verifyUserLoggedIn`, `maxAgeHours` (on a setup) | No — render-only, outside the compile hash |
| Existing `say`/`teach` narration text, `translations` | No — render-only |
| `enterText.text` value alone | No — render-only |
| `config.setup` (on a target) added, removed, changed, or its login user changed | Yes — folded into the target's compile hash |
| Adding, removing, or reordering a `slide` step | Yes |
| A target step's instruction (`teach` sentence, `click`/`hover`, `enterText.into`, `wait.until`/`state`) | Yes |
| Switching a step's command kind | Yes |

See [Scenario files](scenario-files.md#when-render-is-enough) for the complete list,
including `viewport`/`locale`/`tts.lang` and application drift.

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
