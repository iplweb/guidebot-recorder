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
| `desktop` | No | object / navy background | Background colour for a `desktop` opener step; render-only and not part of the compile hash. |
| `fade` | No | object / disabled | Opt-in fade to/from a flat colour at the two ends of the finished film, render-only. |
| `holdFrameForNarration` | No | boolean / `true` | Freeze the picture during narration instead of recording in real time, render-only. |
| `holdFrameSettle` | No | number / `1.0` | Real seconds recorded before the frame freezes, render-only. |
| `selects` | No | object / built-in shim defaults | DOM select shim that makes a native `<select>`'s option list visible on camera; only `mode` affects the compile hash. |

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
is **not cosmetic**: the `setup` path participates in the target's compile hash
(see the [recompile matrix](#recompile-matrix)), so adding, removing, or
repointing `setup` re-resolves the target. (Changing the login user alone
refreshes the cached session but does not itself recompile the target sidecar —
render's live identity check catches any resulting DOM drift with a loud error.)

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

### `desktop`

Render-only appearance for a [`desktop`](#desktop_1) opener step. Only the
background colour lives here — it is a film-wide look, so every `desktop` step in
a film matches without repeating it. Not part of the config hash: the step
compiles to nothing.

| YAML field | Default | Meaning |
|---|---:|---|
| `color` | `#1f3a63` (navy) | Background CSS color behind the desktop icon. |

```yaml
config:
  desktop:
    color: "#1f3a63"
```

### `fade`

Render-only, opt-in fade from/to a flat colour at the film's two ends. Off by
default: enabling it forces a re-encode in the final mux (a fade cannot be
applied to a copied stream), so a scenario that does not ask for one keeps
today's output byte-identical. Not part of the config hash — enabling or
changing a fade never requires a recompile.

```yaml
config:
  fade:
    enabled: true
    in: 0.6
    out: 1.0
```

| YAML field | Default | Meaning |
|---|---:|---|
| `enabled` | `false` | Turn the fade on. |
| `in` | `0.6` | Seconds to fade in from `color` at the start. |
| `out` | `0.8` | Seconds to fade out to `color` at the end. |
| `color` | `black` | The flat colour faded to/from — an ffmpeg color name or `0xRRGGBB`. |
| `audio` | `true` | Fade every narration bed in step with the picture. Turn off only when the audio must run to its last sample under a picture that still cuts to `color`. |

`in` and `out` may each be `0`, which drops that end's transition. Their sum must
not exceed the film's length, or the render fails loudly rather than clip a fade
into nothing.

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

### `selects`

Settings for the DOM select shim used by the [`select`](#select) step. All fields
are optional; omit the whole `selects:` block to keep the built-in shim behavior.

```yaml
selects:
  mode: shim            # shim (default) | native
  settleMs: 1000
  maxVisibleOptions: 8
  openHoldMs: 350
```

| YAML field | Default | Meaning |
|---|---:|---|
| `mode` | `shim` | Global escape hatch. `shim` replaces raw `<select>` elements with the DOM overlay so their option lists are visible on camera; `native` falls back everywhere to the collapsed control — the cursor still travels to it and clicks, but the list never unfurls and the value changes at once. A per-step `select.mode: native` opts one control out of a global `shim`; the reverse is not possible, because `native` injects no shim script for a step to opt back into, and a step that asks for it is rejected when the scenario loads. |
| `settleMs` | `1000` | Milliseconds to wait after page load before classifying each `<select>` as raw or already enhanced. Gives the page's own select2/Tom Select/Chosen initialization time to hide or replace the original control before the shim decides whether to touch it. `0` switches the window off — correct only on a site that enhances nothing, where waiting cannot help. |
| `maxVisibleOptions` | `8` | Number of options shown in the unfurled list at once before it scrolls internally. |
| `openHoldMs` | `350` | Milliseconds the unfurled list stays open for the viewer to read before the cursor moves to the chosen option. |

Only `mode` affects what gets compiled: changing it to `native` changes what the
resolver drives, so — like `config.setup` — it participates in the config hash only
when it differs from the default, and forces a recompile (see the
[recompile matrix](#recompile-matrix)). `settleMs`, `maxVisibleOptions`, and
`openHoldMs` are cosmetic render-time tuning, like `cursor` or `popup`: changing
them never requires a recompile.

## `steps`

`steps` is an ordered list. A step may contain:

- exactly zero or one **main command** from `teach`, `navigate`, `click`, `hover`,
  `enterText`, `select`, `scroll`, `wait`, `slide`, `desktop`, and `closeWindow`;
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
| `select` | Yes, `from` only | Only accompanying `say` |
| `scroll` | No | Only accompanying `say` |
| numeric `wait` | No | Only accompanying `say` |
| conditional `wait` | Yes, `until` | Only accompanying `say` |
| `slide` | No | Only accompanying `say`; on-screen text is shown, not spoken |
| `desktop` | No | Only accompanying `say` |
| `closeWindow` | No | Only accompanying `say` |

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

### `select`

```yaml
- select:
    from: "the report type dropdown"
    option: "tabela"
  say: "From the report type list I choose the table format."
```

Choose an option from a dropdown. `from` is a semantic target instruction sent to
the reasoner and must resolve to a native `<select>` element — resolving to a
non-select control (a custom `role="combobox"` widget, a button) is a `not_select`
validation error. `option` is the visible label of the option to pick; it is shown,
never spoken, and is **not** environment-substituted.

Validation also checks that the resolved `<select>` actually **offers** the requested
option; if it does not, the target is rejected with `option_missing`, and the message
lists the labels the element really has. This is the semantic safety net for dropdowns
with no accessible name, which the resolver can only address positionally
(`combobox nth=N`): that index shifts with the state of the DOM, so without the check a
wrong-but-plausible `<select>` would pass validation and the mistake would only surface
later — as a compile-time timeout, or as a render that clicks the wrong control on
camera. The comparison collapses whitespace and is then case-sensitive: exactly the
rule every execution path applies (see below), so validation never refuses a control
Guidebot could have driven, and never blesses one it could not.

The check applies to exactly the controls whose own `<option>` elements are what
Guidebot will search at render time — a shimmed `<select>` and a natively-visible
listbox (`multiple` / `size="2"`). **A `<select>` the page has already enhanced is
exempt**, because Guidebot drives such a widget through the page's own DOM list and
never through the hidden original's options: a select2 dropdown backed by AJAX
legitimately carries no options at all until the user opens it, and rejecting it
would refuse a control Guidebot can drive perfectly well. A `<select>` that carries
no options *and* is not enhanced is still rejected, with a message saying there is
nothing to choose from.

A native `<select>`'s option list is drawn by the operating system, so no
browser-automation tool — Playwright included — can unfurl or screenshot it. To make
the choice visible anyway, Guidebot injects a DOM shim (`config.selects`, described
below) that replaces the raw control with an overlay whose list genuinely opens
downward in the page. During `render` the cursor glides to the control and clicks
it, the list unfurls, the cursor glides to the chosen row, and clicks it — two
visible beats instead of an invisible value change. During `compile` the value is
set directly with no animation, since compilation is meant to be fast, not pretty.
Either way the element ends on `option`, so later steps and the render agree.

**Widgets the page has already enhanced are deliberately left alone.** If the
target application replaces its `<select>` with its own dropdown — select2, Tom
Select, Chosen, or anything shaped the same way (a hidden or invisible original
`<select>` plus a sibling widget) — Guidebot does not shim it: that widget's list
is already DOM and already records correctly. The same two-beat choreography drives
it directly instead — the visible control associated with the hidden select (found
by its `aria-controls`/`aria-owns`, an `aria-labelledby`/`aria-describedby`
back-reference, or, failing both, the nearest visible sibling), then the option row
that appears once it opens.

**A `<select multiple>` or `<select size="2">` needs no shim and gets none.** It
already draws its option list in the page rather than as an OS popup, so there is
nothing to replace — and because its rows are laid out on screen from the start,
there is also no list to unfurl. The choreography is a single beat: the cursor
glides straight to the `<option>` and clicks it, scrolling the listbox to it first
if it sits below the fold. The control keeps its own native appearance throughout.

As always for `select`, this picks **one** option: clicking it deselects whatever
else was selected, exactly as setting the value directly does. There is no way to
choose several options in one step.

`option` is matched against the option's visible label with whitespace collapsed,
and is then **case-sensitive** — the same rule on every shape of control, so one
scenario cannot resolve one way on a shimmed select and another on a widget the
page enhanced itself.

If nothing can be clicked — no visible control associated with an already-enhanced
select, a select that is on screen but carries no option list Guidebot can open,
no shim installed in that context at all, or no row matching `option` after
opening the list — the run **fails** rather than silently setting the value; a
widget the shim cannot drive is not one Guidebot will pretend to have shown. The
error message says which of those situations it is in, naming the marker class
where one is the cause.

`compile` probes an enhanced widget up front as well, but the probe answers a
narrower question than "is this drivable": it asks whether *any* visible control
can be associated with the hidden select, not whether the one it found is the
right one. So it does catch the **no visible control** case — a Tom Select-style
hidden original whose widget is missing fails at compile, not several minutes
into a render. It does **not** catch the **wrong control** case: the last step of
the association heuristic is "the nearest following sibling with a box", so a
select whose real widget lives elsewhere in the document can be matched to an
unrelated neighbouring element. Compile still passes — it sets the value with
`select_option`, never through the widget — and the render is where the mistake
shows: the cursor clicks that unrelated element on camera, waits for an option
row that never appears, and the step fails there. If a `select` step compiles but
fails partway through a render with "the option did not appear", an
`aria-controls` (or `aria-owns`) on the `<select>` naming its real widget is the
fix: that is the heuristic's first and strongest signal.

**A click that lands on nothing fails too.** After clicking the row, Guidebot
reads the `<select>` back and checks that it really shows `option`; if it does
not, the step fails instead of reporting success. That covers the cases where
there *is* a row to click and clicking it achieves nothing — a `disabled` option,
or a widget whose list is accompanied by a toast or live region repeating the same
label. Without the read-back such a run would finish green and produce a video in
which the dropdown visibly never changes.

For a widget the shim genuinely cannot drive — a search-as-you-type dropdown that
loads its options over the network, for instance — use the per-step **`mode:
native`** escape hatch. The list never unfurls: the cursor still glides to the
collapsed control and clicks it, and the value changes at once, the moment the
cursor arrives — there is no intermediate stepping animation to watch, only the
travel and the change.

Earlier versions animated that change by pressing `ArrowDown`/`ArrowUp` on the
collapsed control. That is gone because it is **platform-dependent**, not because
arrow keys are useless: measured with this project's pinned Playwright on macOS,
headless and headed, focusing a native `<select>` and pressing `ArrowDown` twice
leaves `selectedIndex` at `0` and fires no `change` — macOS binds those keys on a
closed dropdown to opening the OS popup. On Linux and Windows Chromium they do
step the value and do fire `change`. One scenario would therefore have produced
one film on a Mac and a different one on a Linux CI runner, from the same
compiled artifact — so `mode: native` now behaves identically everywhere: travel,
ripple, value.

`mode` also has a global form, `config.selects.mode` (see
the `selects` config block below); the per-step value defaults to it and overrides
it for one stubborn control in an otherwise fine scenario. Under a global `shim`
the override also *removes* the shim from that one control before driving it, and
keeps it off for the rest of the recording; every other select on the page keeps
its DOM list:

```yaml
- select:
    from: "the province dropdown"
    option: "Mazowieckie"
    mode: native          # optional; defaults to config.selects.mode
```

The override is **one-way**. `config.selects.mode: native` is not a default a step
can override in the other direction: it decides whether the shim script is
injected into the browser at all, so underneath it there is no shim for a step to
opt back into. A step that asks for `mode: shim` while the global mode is `native`
is rejected when the scenario loads, with the file, the line number and the
offending YAML fragment (see
[Reading a step message](troubleshooting.md#reading-a-step-message)) — rather than
failing several minutes into a render, after the cursor has already clicked
something unrelated on camera.

Pair a `select` step with a `say` such as "from this list I choose …" to narrate
the intent.

### `scroll`

```yaml
- scroll: down                    # up | down | top | bottom
- scroll: { to: down, amount: 300 }   # object form; amount in pixels
  say: "I scroll down to reveal the results preview."
```

Scroll the page — a render-only visual with no agent target, like a numeric `wait`.
`to` is `up`, `down`, `top`, or `bottom`; the string shorthand `scroll: down` is
accepted for all four. `amount` (pixels) tunes an `up`/`down` scroll and is ignored
for `top`/`bottom`; without it, `down`/`up` moves by most of a viewport.

Its purpose is to bring below-the-fold content into view so the recording shows it —
in particular content the resolver **cannot** target, such as a live-preview
`<iframe>`. (A native select's option list used to belong on that list and no
longer does: the shim renders it into the DOM, so `select` drives it directly.)
The cursor still cannot enter an iframe,
but the scroll brings the iframe into frame. With an overlay (render) the scroll is
an animated glide; during `compile` it jumps directly. Because it resolves no
element, `scroll` needs no `compile` for its own sake and takes no `optional`.

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

Adding, removing, or reordering a `slide` step changes the step count, so it
**needs `guidebot compile`**; render preflights the step count against the sidecar and
fails loudly otherwise. The same holds for `closeWindow` (below).

### `closeWindow`

```yaml
- teach: "Click the link that opens in a new tab"
- say: "We've read it; now back."
- closeWindow: true
```

Closes the **active** window and returns to the one that opened it. It accepts only
`true`; `closeWindow: false` is a validation error, not a silent no-op. With no window
open the step fails.

A new window appears on its own when a click on the page opens one — via `window.open`
or a `target="_blank"` link. Guidebot recognises it by its `opener()`, so a link with
`rel="noopener"` (which nulls `opener()`) is **not** recognised as opening a window. A
window that fills the whole canvas (e.g. a `target="_blank"` tab that requested no size)
is shown full-frame with its own address bar; a smaller `window.open` window keeps the
floating presentation. The scenario never opens a window itself — there is no "open a
window" command.

Like `slide`, `closeWindow` changes the step count, so it **needs `guidebot compile`**.
Full example: [`examples/newwindow/`](https://github.com/iplweb/guidebot-recorder/tree/main/examples/newwindow).

### `desktop`

```yaml
- desktop:
    icon: chrome        # optional; built-in name or a path to your own image
    label: "Open the browser"   # optional; caption under the icon
    hold: 1.0            # optional; seconds to hold once the window has opened
  say: "Let's open the browser."   # optional narration
```

A simulated desktop opener for the start of a film: the cursor arcs to a browser
icon, double-clicks it, and a window grows out of the icon to reveal the address
bar the next `navigate` step then types into. Visual-only, like `slide` — it
compiles to nothing, so it **needs `guidebot compile`** purely because it adds or
moves a step (render checks the step count).

The desktop's background colour is a film-wide render setting, `config.desktop.color`
(default navy, `#1f3a63`), not a per-step field, so every `desktop` step in a film
matches.

`icon` accepts a **built-in name** or a **path to your own file**
(`.svg`/`.png`/`.jpg`/`.gif`/`.webp`, relative to the scenario's directory). The
built-in icons are deliberately **generic, hand-drawn** stand-ins, not real browser
logos — those are trademarks and this package is redistributable. The name only says
which browser the icon evokes:

| Name | Drawing |
|---|---|
| `chrome`, `browser` | a colored ring with a blue center |
| `firefox`, `flame` | a flame |
| `iexplore`, `edge`, `legacy` | a blue "e" |
| `globe` | a plain globe |

Point `icon` at your own file to use a real logo — nothing is then distributed with
the package.

`label` defaults to the literal string `"Przeglądarka internetowa"` (Polish for
"web browser") — the built-in default was never translated, so give an English
scenario its own `label` explicitly rather than relying on the default.

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
| `desktop.color`, `fade` | No — render-only |
| `holdFrameForNarration`, `holdFrameSettle` | No — render-only |
| `verifyUserLoggedIn`, `maxAgeHours` (on a setup) | No — render-only, outside the compile hash |
| Existing `say`/`teach` narration text, `translations` | No — render-only |
| `enterText.text` value alone | No — render-only |
| `select.option` value alone | No — render-only |
| `config.selects.settleMs`, `maxVisibleOptions`, `openHoldMs` | No — render-only |
| `config.setup` (on a target) added, removed, or repointed | Yes — the setup path is folded into the target's compile hash |
| `config.selects.mode` switched between `shim` and `native` | Yes — folded into the config hash, like `config.setup`, only when non-default |
| Adding, removing, or reordering a `slide` or `desktop` step | Yes |
| A target step's instruction (`teach` sentence, `click`/`hover`, `enterText.into`, `select.from`, `wait.until`/`state`) or a step's own `select.mode` | Yes |
| Switching a step's command kind | Yes |
| A frozen `text=` target that matches an `<option label="…">` or `<optgroup label="…">` string | Rarely — see below |

See [Scenario files](scenario-files.md#when-render-is-enough) for the complete list,
including `viewport`/`locale`/`tts.lang` and application drift.

**One narrow case where an artifact compiled before the select shim can need a
recompile.** The shim draws the option list — and the collapsed control's own
label — as real DOM text at `<body>` level. For an option's *text content* that
changes nothing: the same string was already in the DOM, inside the `<select>`,
so no target can start matching twice because of it. The exception is the `label`
**attribute**: `<option label="Short">` is rendered by its attribute rather than
its text, and `<optgroup label="…">` headings put their attribute on screen as
text that was never DOM text before. A
step frozen as a `text=` target whose string happens to equal one of those
attributes can now match in two places, fail its reuse check with `not_unique`,
and need `guidebot compile` once. Nothing else is exposed: `role=` targets are
unaffected because every shim overlay is `aria-hidden` and invisible to the
accessibility tree, and `testid=` and `label=` targets are unaffected because the
overlays carry neither a test id nor a form label. Re-running `guidebot compile`
resolves it; the step then freezes against the DOM the render will actually see.

## Environment substitution

`${NAME}` is expanded only in:

- string-form `navigate` or object-form `navigate.url`;
- `enterText.text`.

Substitution does not run in `baseUrl`, `say`, `teach`, `translations`, target
instructions, `select.option`, or any TTS/config field.

A variable may appear more than once. Missing variables raise an error. `$${` escapes
a literal `${` sequence:

```yaml
- enterText: { into: "the template field", text: "$${USER}" }
```

This fills the literal text `${USER}`.

The generated sidecar uses compiler schema version 2 and must be regenerated rather
than edited. See [Scenario files](scenario-files.md#the-generated-sidecar) for its
layout and lifecycle.
