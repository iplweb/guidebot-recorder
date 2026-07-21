# guidebot-recorder

[![CI](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/iplweb/guidebot-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

[English](#english) · [Polski](#polski)

## English

Guidebot Recorder compiles a plain-language YAML scenario into a deterministic,
narrated training video. Playwright opens the site, follows the ordered flow,
animates a visible cursor, and records the result. TTS narration is then mixed into
the final `.mp4`.

### Two-phase compiler

```text
login.scenario.yaml ── compile (AI) ──▶ login.compiled.yaml ── render (no LLM) ──▶ login.mp4
       authored source                     frozen targets                         video
```

- `compile` runs the flow and resolves plain-language element descriptions into
  validated, structural Playwright actions. It writes a separate, versioned sidecar
  containing targets, identities, fingerprints, and observed pop-up behavior.
- `render` replays that sidecar without an LLM, verifies identity for ordinary
  click/hover/type actions, animates the cursor, optionally draws a macOS-style
  browser bar, records Chromium, synthesizes/caches one or more narration tracks, and
  uses ffmpeg to create the final video. Conditional waits do not get the live
  identity check.
- Recompilation is incremental. Unchanged target steps can be reused; `--force`
  resolves everything again.
- Commit both `*.scenario.yaml` and `*.compiled.yaml`. The source remains readable,
  while the sidecar makes target changes reviewable.

### Which agents can compile navigation?

**Out of the box, only Codex CLI can run the `guidebot compile` AI step.**

| Task or backend | Supported now | What that means |
|---|---:|---|
| Author a `.scenario.yaml` | Any human or external agent | Write the ordered URLs and steps, then run `guidebot validate`. |
| Codex CLI | Yes | The only backend wired into the `guidebot compile` command. |
| Custom Python `Reasoner` | API only | Implement the protocol and call `run_compile(...)` from your own runner. |
| Claude, Gemini, OpenCode, direct model APIs | No built-in adapter | They can author YAML, but cannot be selected as the compiler without new integration code. |
| Playwright Chromium | Yes, not AI | It inspects, validates, and operates the page. |
| Video rendering | No agent | Rendering uses the frozen sidecar and makes zero LLM calls. |

The distinction matters: Guidebot does **not** ask an agent to invent or discover a
complete route through a website. You author the ordered `navigate`, `teach`,
`enterText`, and other steps. During compilation, Codex maps one instruction on the
currently active page to structured action/target data. For a non-sensitive literal
typing instruction in `teach`, it may also return `inputText`; Guidebot validates and
freezes that literal. Codex receives a text candidate snapshot that omits form-field
values, does not control the browser or switch windows, and runs without web search,
shell tools, plugins, skills, MCP, browser/computer-use, or subagents. Text that the
application reflects into the DOM or accessible names can still enter a later
snapshot.

Guidebot currently exposes no `--reasoner` or `--model` option. It also ignores the
user Codex configuration for this constrained call, so a model cannot be selected in
`~/.codex/config.toml` for Guidebot compilation. See
[Compiling agents](docs/en/compiling-agents.md) for the exact boundary and the custom
`Reasoner` extension point.

### Five CLI commands

| Command | Purpose | Agent use |
|---|---|---:|
| `guidebot validate SCENARIO` | Validate one scenario without opening Chromium. | None |
| `guidebot compile SCENARIO` | Compile one scenario into its adjacent sidecar. | Codex on cache misses |
| `guidebot render SCENARIO --out FILE.mp4` | Render one scenario, optionally with several audio tracks. | None |
| `guidebot guide SCENARIO --out FILE.pdf` | Build a landscape PDF guide with annotated screenshots and step narration. | None |
| `guidebot compile-set MANIFEST` | Compile every complete localized scenario listed in a render-set manifest. | Codex on cache misses |
| `guidebot render-set MANIFEST --output-dir DIR` | Render one single-audio MP4 per localized variant. | None |

`compile-set` and `render-set` process variants in manifest order. Each variant has a
fresh browser context and its own normal `*.compiled.yaml`; targets and sessions are
never shared between languages.

### Install

Requirements: Python 3.12+, [uv], Playwright Chromium, ffmpeg/ffprobe, and — for
`compile` or `compile-set` — an installed and authenticated [Codex CLI].

```bash
uv sync
uv run playwright install chromium

# Install Codex CLI (one option) and authenticate it
npm install -g @openai/codex
codex login
codex login status
```

Install ffmpeg with `brew install ffmpeg` on macOS or
`sudo apt install ffmpeg` on Debian/Ubuntu. Codex can reuse either a ChatGPT sign-in
or API-key authentication; API-key use is billed through the API account.

### Quick start

```bash
export DEMO_EMAIL=user@example.com

uv run guidebot validate examples/login.scenario.yaml
uv run guidebot compile examples/login.scenario.yaml --headed -v
uv run guidebot render examples/login.scenario.yaml --out out/login.mp4 -v
```

Use a staging site and a disposable account: both `compile` and `render` really
navigate, click, and enter values. If the application changed, or render reports an
identity mismatch, rebuild targets with `guidebot compile ... --force`.

### Source scenario

```yaml
config:
  title: "Logging in to the system"
  baseUrl: https://example.com
  viewport: { width: 1280, height: 720 }
  locale: en-US
  tts: { provider: edge, voice: en-US-GuyNeural, lang: en-US }
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true
steps:
  - navigate: /login
  - say: "Welcome. I'll show you how to log in."
  - enterText:
      into: "the email address field"
      text: "${DEMO_EMAIL}"
    say: "Now I'm entering the email address."
  - teach: "Click the Log in button"
  - wait: { until: "the dashboard to appear", state: visible, timeout: 10 }
```

`${ENV_VAR}` substitution works only in string `navigate`, object `navigate.url`, and
`enterText.text`. A `.env` file is not loaded automatically; export variables into
the command environment.

### Optional browser chrome

The optional `config.chrome` block is render-only and defaults to `enabled: false`,
so existing scenarios keep their current output. When enabled, `showUrl` controls
whether the address pill is visible and `typeOnNavigate` controls whether a
`navigate` step without an explicit `type` override types its URL character by
character before loading it. Both default to `true`. Appearance can be overridden
with `height` (default `56`), `barColor`
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
secret-bearing URLs. The overlay is installed on the initial `about:blank` page;
place the first `navigate` before introductory narration if that blank address should
not appear while the intro is spoken.

### Slide cards, sound, typing animation, and a bigger cursor

A few render-only, opt-in polish features round out a video:

- **`slide` step** — a full-frame title/subtitle/notes card shown anywhere in the
  flow, without touching the underlying page. Its on-screen text is shown, not
  spoken; narration comes from a separate `say`. Unlike the config blocks below,
  adding, removing, or reordering a `slide` step changes the step count and needs
  `guidebot compile`.
- **`config.typing`** — `animate: true` types `enterText`/`teach` input
  character-by-character during render instead of pasting it instantly; `speed` sets
  the delay per character (default `60` ms).
- **`config.sound`** — `enabled: true` mixes a built-in recording of a low-profile
  keyboard and a clearly audible two-stage mouse click under the narration on every
  language track. Repeated keys receive tiny deterministic pitch/level variations
  instead of looping one identical sound. No author-supplied files.
- **`config.intro`** — `enabled: true` opens the film with a title card built from
  `config.title` plus `subtitle`/`notes`, instead of today's blank white first frame.
- **Bigger cursor** — `config.cursor.width`/`height` control the pointer's size
  (defaults `34`/`46`); scale both up, e.g. `46`/`62`, for a bigger, easier-to-follow
  pointer. The click ripple's own look is now configurable under `config.cursor.click`
  (`color`, `scale`, `flash`). The cursor also now starts at the centre of the
  viewport on every render, not the top-left corner.

All of these — including the bigger cursor and its ripple, typing animation, sound,
and the intro card — are render-only, except adding/removing/reordering a `slide`
step, which needs `guidebot compile`. See the
[Scenario YAML reference](docs/en/scenario-reference.md#recompile-matrix) for the
full picture.

### Pop-up windows and literal `teach` typing

When an actual click opens one new Playwright page — for example `window.open()` or a
`target="_blank"` link — Guidebot follows it automatically. The scenario needs no
window name or switch command: subsequent steps compile and render against the
pop-up, and control returns to the main page after a scenario action closes it. A
pop-up may also remain open through the end of the scenario.

The current contract supports **one pop-up lifecycle in the whole scenario**. A
nested, simultaneous, second sequential, unexpected, or wrong-opener page fails
loudly. The main page must stay open, and a timer-driven/asynchronous pop-up close is
not supported. Iframe content of any origin, pre-existing named-window selection,
downloads, and arbitrary tab switching remain outside the supported scope.

The final film cuts full-frame between the separately recorded viewports:
`main -> pop-up -> main`. Native Chromium tabs and window controls are not recorded;
the synthetic cursor and optional browser bar are rendered inside both pages.

`teach` may infer `click`, `hover`, or a literal `type` action. For typing, Codex must
return a non-empty `inputText` copied verbatim from the trusted instruction. Guidebot
validates the target, rejects recognized sensitive instructions and password/OTP-like
fields, stores the literal as `input_text` in the sidecar, and replays it without an
LLM. Never put credentials, tokens, placeholders, or other secrets in `teach`; use
`enterText.text: "${ENV_VAR}"` instead.

### Pre-recording setup (cached login session)

Some videos should begin with the site *already* prepared — logged in, cookie
banner accepted, tenant chosen — without that preparation appearing on the film.
Because recording is armed when the browser context is created, ordinary
login/cookie steps would end up in the video. Pre-recording setup runs that
preparation on a separate, **non-recording** context and reuses the resulting
browser session, so the target scenario can be recorded already logged in.

A **target** scenario points at a **setup** scenario — an ordinary,
already-compiled `*.scenario.yaml`, typically one that teaches logging in — via
`config.setup`, then omits its own login steps:

```yaml
# target.scenario.yaml
config:
  title: "A tour of the dashboard"
  baseUrl: https://example.com
  setup: teach-login.scenario.yaml   # path, relative to THIS scenario
steps:
  - navigate: /dashboard             # already logged in — no login steps here
  - teach: "Open the Reports section"
```

The setup scenario declares the health-check on its **own** `config`:

```yaml
# teach-login.scenario.yaml
config:
  baseUrl: https://example.com
  verifyUserLoggedIn: "Sign out"     # string shorthand for {containsText: "Sign out"}
  # full form (only containsText is required):
  # verifyUserLoggedIn:
  #   containsText: "Sign out"
  #   url: /dashboard                # optional; defaults to the target's baseUrl
  #   timeout: 8                     # optional; seconds, default 8
  maxAgeHours: 12                    # optional TTL for the cached session
steps:
  - navigate: /login
  - enterText: { into: "the email field", text: "${DEMO_EMAIL}" }
  - enterText: { into: "the password field", text: "${DEMO_PASSWORD}" }
  - teach: "Click the Log in button"
```

`verifyUserLoggedIn` accepts a string (shorthand for `containsText`) or an
object. The match is a plain, **case-sensitive substring** of the page's visible
`body.innerText`. Choose text that renders **only when authenticated** — a
username is the robust choice; a logged-out footer that happens to contain the
word would false-positive, because the match has no word boundaries.
`maxAgeHours` is an optional time-to-live. If you set **neither**
`verifyUserLoggedIn` nor `maxAgeHours`, a present cache is trusted until you pass
`--force`, and the tool prints a loud warning.

**Compile the setup scenario first.** `guidebot compile TARGET` and
`guidebot render TARGET` both auto-establish or reuse the session before their
own work when the target has `config.setup` — no separate step in the common
case. But the setup scenario must be compiled beforehand
(`guidebot compile teach-login.scenario.yaml`); if it is not, compile, render, or
`setup` of the target fail loudly and tell you to compile the setup. Establishing
the session **replays the setup's frozen targets and makes zero LLM calls.**

To build or refresh the cached session by hand:

```bash
guidebot setup teach-login.scenario.yaml [--headed] [--force] [--timeout 15] [-v]
```

A plain run with a live cache reuses it (`session reused (already live)`);
otherwise it replays the setup and caches the result
(`session refreshed and cached`). `--force` always rebuilds. `--headed` shows the
browser and, if the automatic replay's health-check fails, pauses so you can
finish logging in by hand (MFA/captcha) before it snapshots.

**Security.** The cache is a Playwright `storage_state` under
`.guidebot/sessions/<key>.json` — a bearer credential. It is written `0600` (dir
`0700`), and the tool auto-writes `.guidebot/sessions/.gitignore` (`*`) so it is
never committed. The cache key folds in a digest of the referenced `${ENV}`
credential values, so changing the login user refreshes the session
automatically.

**Limitations (v1).** Only cookies and `localStorage` are cached; a session kept
in `sessionStorage` or IndexedDB (some OIDC/MSAL SPAs) cannot be cached, and the
tool detects and reports this. The setup and target must share the same origin
(host); cross-origin reuse is a hard error. One language-agnostic session is
reused across localized render-set variants; if a backend pins UI language to the
session, frozen localized labels can mismatch.

### Two multilingual output modes

Guidebot supports two deliberately different workflows:

1. **One video with selectable audio tracks.** Keep one canonical page flow in a
   normal scenario, add alternate `config.audioTracks`, and provide a `translations`
   entry for every alternate language on every narrated `say`/`teach` step. The
   browser is recorded once and the final MP4 contains one video stream plus one
   language-tagged audio stream per configured track. The longest narration for a
   step determines when its shared visual action happens. Translations are render-only
   and are never sent to Codex.
2. **One fully localized video per language.** Create a complete scenario for each
   locale and group them in a `kind: localized-render-set`, `version: 1` manifest.
   Use `compile-set` and `render-set` when URLs, UI labels, target descriptions,
   narration, or pacing differ. Every variant gets its own sidecar, fresh browser
   context, recording, and single-audio MP4.

```bash
# One shared picture with multiple selectable audio streams
uv run guidebot compile examples/multilingual-login.scenario.yaml
uv run guidebot render examples/multilingual-login.scenario.yaml \
  --out out/multilingual-login.mp4

# Independent localized pictures and narration
uv run guidebot compile-set examples/localized-login.render-set.yaml
uv run guidebot render-set examples/localized-login.render-set.yaml \
  --output-dir out/localized-login
```

For embedded multi-audio output, all tracks currently use one provider and every
narrated step must have exactly the required translations; the stock CLI accepts only
the `edge` provider. When `audioTracks` is non-empty, give every track a unique ISO
639-2 `trackLanguage`; the first `config.tts` track is the sole default stream. See
[Multilingual audio](docs/en/multilingual-audio.md) and
[Localized render sets](docs/en/localized-render-sets.md).

### Files

| File | Owner | Commit? |
|---|---|---:|
| `flow.scenario.yaml` | You | Yes |
| `flow.compiled.yaml` | Generated by `compile`; do not hand-edit | Yes |
| `flow.render-set.yaml` | You; groups complete localized scenarios | Yes |
| `out/flow.mp4` | Generated by `render` | Usually no |
| `.guidebot/audio/` | Persistent TTS audio and JSON metadata; delete manually | No |
| `.guidebot/sessions/` | Cached login sessions (bearer credentials); auto-gitignored, delete manually | No |
| `<output-dir>/.guidebot_video/<output-stem>/` | Persistent per-film WebM, composite, and WAV work files; delete manually | No |

The current compiled schema is version 2. Older sidecars may still parse for migration
purposes, but compile/replay treats them as stale and requires recompilation.

After target instructions, routes, command kinds, step alignment, or target-relevant
config changes, complete `guidebot compile` successfully before rendering. Changes
limited to existing narration, alternate tracks/translations, `cursor`, or `chrome`
are render-only. Before synthesis or recording, render rejects a sidecar with the
wrong source name, slot count, compiler version, command/action kind, instruction,
selected config fingerprint, wait state, readiness, or frozen `teach` literal. It
then checks live identity for ordinary click/hover/type actions; conditional waits
still skip that live identity check. Routes/`baseUrl` and external application drift
are not fully fingerprinted, so use `compile --force` after those changes or a pop-up
lifecycle/identity error.

`.guidebot/audio/` retains MP3 files and JSON metadata containing narration text. A
per-film work directory can retain the main and pop-up WebMs, a pop-up composite MP4,
and full-duration `bed-<language>.wav` files. Treat both directories as potentially
sensitive and remove them manually when no longer needed.

The full English guide covers [getting started](docs/en/getting-started.md),
[scenario files](docs/en/scenario-files.md), the complete
[YAML reference](docs/en/scenario-reference.md), [CLI](docs/en/cli-reference.md),
[multilingual audio](docs/en/multilingual-audio.md),
[localized render sets](docs/en/localized-render-sets.md), and
[troubleshooting](docs/en/troubleshooting.md).

Current beta limitations include only one automatically followed pop-up lifecycle,
no iframe-content support regardless of origin, no explicit/named tab switching, no
recorder/discovery command, no auto-heal, only the Edge TTS adapter in the stock CLI,
and no selectable reasoner backend. See the docs before choosing a production flow.

---

## Polski

Guidebot Recorder kompiluje scenariusz YAML napisany zwykłym językiem do
deterministycznego filmu szkoleniowego z lektorem. Playwright otwiera serwis,
realizuje uporządkowany przebieg, animuje widoczny kursor i nagrywa wynik. Na końcu
program dodaje narrację TTS do pliku `.mp4`.

### Kompilator dwufazowy

```text
login.scenario.yaml ── kompilacja (AI) ──▶ login.compiled.yaml ── render (bez LLM) ──▶ login.mp4
       źródło autora                           zamrożone cele                         film
```

- `compile` wykonuje scenariusz i zamienia opisy elementów na zweryfikowane,
  strukturalne akcje Playwrighta. Zapisuje w osobnym, wersjonowanym sidecarze targety,
  tożsamości, fingerprinty i zaobserwowane zachowanie popupu.
- `render` odtwarza ten plik bez LLM, sprawdza tożsamość zwykłych akcji
  kliknięcia/hover/wpisania tekstu, animuje kursor, opcjonalnie dodaje pasek
  przeglądarki, nagrywa Chromium, generuje jedną lub wiele ścieżek narracji i używa
  ffmpeg do utworzenia filmu. Warunkowy `wait` nie ma kontroli tożsamości na żywej
  stronie.
- Ponowna kompilacja jest inkrementalna. Niezmienione kroki mogą korzystać z pamięci podręcznej;
  `--force` rozwiązuje wszystkie cele od nowa.
- Dodawaj do repozytorium zarówno `*.scenario.yaml`, jak i `*.compiled.yaml`. Źródło
  pozostaje czytelne, a zmiany celów są widoczne podczas przeglądu kodu.

### Jakich agentów można użyć do kompilowania nawigacji?

**Bez dopisywania kodu fazę AI polecenia `guidebot compile` obsługuje tylko Codex CLI.**

| Zadanie lub backend | Wsparcie teraz | Co to znaczy |
|---|---:|---|
| Pisanie `.scenario.yaml` | Dowolny człowiek lub zewnętrzny agent | Zapisuje kolejność adresów i kroków; potem uruchamiasz `guidebot validate`. |
| Codex CLI | Tak | Jedyny backend podłączony do polecenia `guidebot compile`. |
| Własny `Reasoner` w Pythonie | Tylko przez API | Implementujesz protokół i wywołujesz `run_compile(...)` we własnym runnerze. |
| Claude, Gemini, OpenCode, bezpośrednie API modeli | Brak gotowego adaptera | Mogą napisać YAML, ale bez nowej integracji nie da się ich wybrać jako kompilatora. |
| Playwright Chromium | Tak, ale to nie AI | Odczytuje, sprawdza i obsługuje stronę. |
| Render filmu | Bez agenta | Używa zamrożonego sidecara i wykonuje zero wywołań LLM. |

To ważne rozróżnienie: Guidebot **nie** zleca agentowi wymyślenia ani odkrycia całej
trasy po serwisie. To autor zapisuje uporządkowane kroki `navigate`, `teach`,
`enterText` i pozostałe. Podczas kompilacji Codex mapuje jedną instrukcję na aktualnie
aktywnym widoku na strukturalne dane akcji i targetu. Dla niewrażliwej, literalnej
instrukcji wpisania tekstu w `teach` może też zwrócić `inputText`, który Guidebot
waliduje i zamraża. Codex dostaje tekstowy wyciąg kandydatów bez wartości pól
formularza; nie steruje przeglądarką ani nie przełącza okien i działa bez wyszukiwania
w sieci, powłoki, pluginów, skills, MCP, browser/computer-use ani subagentów. Tekst,
który aplikacja pokaże później w DOM lub nazwie dostępności, może jednak trafić do
kolejnego wyciągu.

Guidebot nie ma obecnie opcji `--reasoner` ani `--model`. Dla tego ograniczonego
wywołania ignoruje też konfigurację użytkownika Codexa, więc modelu nie można wybrać
w `~/.codex/config.toml`. Dokładny opis znajdziesz na stronie
[Agenci kompilujący](docs/pl/compiling-agents.md).

### Pięć poleceń CLI

| Polecenie | Zastosowanie | Użycie agenta |
|---|---|---:|
| `guidebot validate SCENARIUSZ` | Waliduje jeden scenariusz bez uruchamiania Chromium. | Brak |
| `guidebot compile SCENARIUSZ` | Kompiluje jeden scenariusz do sąsiedniego sidecara. | Codex przy braku ważnego celu |
| `guidebot render SCENARIUSZ --out PLIK.mp4` | Renderuje jeden scenariusz, opcjonalnie z wieloma ścieżkami audio. | Brak |
| `guidebot guide SCENARIUSZ --out PLIK.pdf` | Buduje krajobrazowy przewodnik PDF z anotowanymi zrzutami ekranu i narracją kroków. | Brak |
| `guidebot compile-set MANIFEST` | Kompiluje kompletne, zlokalizowane scenariusze z manifestu zestawu. | Codex przy braku ważnego celu |
| `guidebot render-set MANIFEST --output-dir KATALOG` | Renderuje osobny, jednościeżkowy MP4 dla każdego wariantu językowego. | Brak |

`compile-set` i `render-set` przetwarzają warianty w kolejności z manifestu. Każdy
wariant ma świeży kontekst przeglądarki i własny zwykły `*.compiled.yaml`; targety ani
sesje nie są współdzielone między językami.

### Instalacja

Wymagania: Python 3.12+, [uv], Chromium dla Playwrighta, ffmpeg/ffprobe oraz — dla
`compile` lub `compile-set` — zainstalowany i uwierzytelniony [Codex CLI].

```bash
uv sync
uv run playwright install chromium

# Jedna z metod instalacji Codex CLI oraz logowanie
npm install -g @openai/codex
codex login
codex login status
```

Na macOS zainstaluj ffmpeg przez `brew install ffmpeg`, a na Debianie/Ubuntu przez
`sudo apt install ffmpeg`. Codex może korzystać z logowania kontem ChatGPT albo z
klucza API; użycie klucza API jest rozliczane na koncie API.

### Szybki start

```bash
export DEMO_EMAIL=user@example.com

uv run guidebot validate examples/login.scenario.yaml
uv run guidebot compile examples/login.scenario.yaml --headed -v
uv run guidebot render examples/login.scenario.yaml --out out/login.mp4 -v
```

Używaj środowiska testowego i jednorazowego konta: zarówno `compile`, jak i `render`
naprawdę przechodzą po stronach, klikają i wpisują wartości. Gdy aplikacja się
zmieniła albo render zgłasza niezgodną tożsamość, przebuduj cele przez
`guidebot compile ... --force`.

### Scenariusz źródłowy

```yaml
config:
  title: "Logowanie do systemu"
  baseUrl: https://example.com
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts: { provider: edge, voice: pl-PL-ZofiaNeural, lang: pl-PL }
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true

steps:
  - navigate: /login
  - say: "Pokażę teraz, jak zalogować się do systemu."
  - enterText:
      into: "pole adresu e-mail"
      text: "${DEMO_EMAIL}"
    say: "Wpisuję adres e-mail."
  - teach: "Kliknij przycisk Zaloguj"
  - wait: { until: "pojawienie się pulpitu", state: visible, timeout: 10 }
```

Podstawianie `${ENV_VAR}` działa tylko w tekstowym `navigate`, obiektowym
`navigate.url` oraz `enterText.text`. Program nie wczytuje automatycznie pliku
`.env`; zmienne trzeba przekazać w środowisku uruchamianego polecenia.

### Opcjonalny pasek przeglądarki

Blok `config.chrome` działa wyłącznie w renderze i domyślnie ma `enabled: false`.
`showUrl` steruje widocznością pola adresu, a `typeOnNavigate` — animowanym wpisywaniem
adresu dla kroku bez jawnego nadpisania `type`. Oba pola domyślnie mają wartość
`true`. Wygląd można zmienić
przez `height` (`56`), `barColor` (`#f3f4f6`), `textColor` (`#374151`), `radius`
(`12`), `showLock` (`true`) oraz kolory kropek: `closeColor`, `minimizeColor` i
`maximizeColor`. Ustawienia nie wchodzą do hasha kompilacji.

Pojedynczy krok może nadpisać animację:

```yaml
- navigate: "/login"                         # dziedziczy typeOnNavigate
- navigate: { url: "/login", type: true }    # wpisz, potem załaduj
- navigate: { url: "/login", type: false }   # załaduj i pokaż od razu
```

`showUrl: false` ukrywa pole adresu i wyłącza opóźnienie pisania, ale pozostawia sam
pasek. Nakładka zajmuje `height` pikseli wewnątrz skonfigurowanego viewportu i może
zmienić responsywny układ strony. Pełny URL, w tym query string i fragment, może
pojawić się w filmie — dla adresów zawierających sekrety wyłącz `showUrl`. Nakładka
jest instalowana już na początkowej stronie `about:blank`; umieść pierwszy `navigate`
przed narracją wstępną, jeżeli nie chcesz pokazywać pustego adresu podczas powitania.

### Plansze (`slide`), dźwięk, animacja pisania i większy kursor

Kilka opcjonalnych funkcji wyłącznie renderu dopełnia film:

- **Krok `slide`** — pełnoekranowa plansza z tytułem/podtytułem/notatkami pokazywana
  w dowolnym miejscu scenariusza, bez naruszania strony pod spodem. Tekst na planszy
  jest wyświetlany, nie czytany; narrację dostarcza osobny `say`. W odróżnieniu od
  poniższych bloków konfiguracji, dodanie, usunięcie lub zmiana kolejności kroku
  `slide` zmienia liczbę kroków i wymaga `guidebot compile`.
- **`config.typing`** — `animate: true` wpisuje tekst `enterText`/`teach` znak po
  znaku podczas renderu zamiast wklejać go od razu; `speed` ustawia opóźnienie na
  znak (domyślnie `60` ms).
- **`config.sound`** — `enabled: true` wmiksowuje pod narrację na każdej ścieżce
  językowej nagrany stuk niskoprofilowej klawiatury i wyraźny, dwuczęściowy klik
  myszy. Kolejne klawisze dostają drobne, deterministyczne różnice wysokości i
  poziomu zamiast zapętlać identyczny dźwięk. Bez własnych plików dźwiękowych.
- **`config.intro`** — `enabled: true` otwiera film planszą tytułową zbudowaną z
  `config.title` oraz `subtitle`/`notes`, zamiast dzisiejszej pustej, białej
  pierwszej klatki.
- **Większy kursor** — `config.cursor.width`/`height` sterują rozmiarem strzałki
  (domyślnie `34`/`46`); zwiększ oba razem, np. do `46`/`62`, dla większego, lepiej
  widocznego kursora. Wygląd rippla po kliknięciu można teraz skonfigurować w
  `config.cursor.click` (`color`, `scale`, `flash`). Kursor zaczyna też każdy render
  na środku viewportu, a nie w lewym górnym rogu.

Wszystkie te funkcje — łącznie z większym kursorem i jego ripplem, animacją pisania,
dźwiękiem i planszą tytułową — są wyłącznie renderowe, poza dodaniem, usunięciem lub
zmianą kolejności kroku `slide`, co wymaga `guidebot compile`. Zobacz
[dokumentację YAML scenariusza](docs/pl/scenario-reference.md#macierz-przebudowy) po
pełny obraz.

### Popupy i literalne wpisywanie przez `teach`

Gdy właściwe kliknięcie otworzy jedną nową stronę Playwrighta — na przykład przez
`window.open()` albo odnośnik `target="_blank"` — Guidebot automatycznie za nią podąża.
Scenariusz nie potrzebuje nazwy okna ani komendy przełączenia: kolejne kroki są
kompilowane i odtwarzane w popupie, a po zamknięciu go przez akcję scenariusza sterowanie
wraca do strony głównej. Popup może też pozostać otwarty do końca scenariusza.

Obecny kontrakt obsługuje **jeden cykl życia popupu w całym scenariuszu**. Popup
zagnieżdżony, równoczesny, drugi sekwencyjny, nieoczekiwany albo otwarty przez złe okno
kończy przebieg jawnym błędem. Główna strona musi pozostać otwarta, a zamknięcie popupu
przez timer lub inną akcję asynchroniczną nie jest obsługiwane. Zawartość iframe'ów
niezależnie od pochodzenia, wybór istniejących nazwanych okien, pobieranie plików i
dowolne przełączanie kart pozostają poza obsługiwanym zakresem.

Końcowy film przełącza pełny kadr między osobnymi nagraniami:
`strona główna -> popup -> strona główna`. Natywne karty i kontrolki okien Chromium
nie są nagrywane; syntetyczny kursor i opcjonalny pasek przeglądarki występują na obu
stronach.

`teach` może wywnioskować `click`, `hover` albo literalną akcję `type`. Przy wpisywaniu
Codex musi zwrócić niepuste `inputText` skopiowane dosłownie z zaufanej instrukcji.
Guidebot sprawdza target, odrzuca rozpoznane instrukcje wrażliwe oraz pola podobne do
hasła/OTP, zapisuje literał jako `input_text` w sidecarze i odtwarza go bez LLM. Nigdy
nie umieszczaj danych logowania, tokenów, placeholderów ani innych sekretów w `teach`;
użyj `enterText.text: "${ENV_VAR}"`.

### Przygotowanie środowiska przed nagraniem (buforowana sesja logowania)

Część filmów powinna zaczynać się z serwisem *już* przygotowanym — zalogowanym,
z zaakceptowanym bannerem cookies, z wybranym tenantem — bez pokazywania tego
przygotowania na filmie. Ponieważ nagrywanie jest uzbrajane przy tworzeniu
kontekstu przeglądarki, zwykłe kroki logowania/cookies trafiłyby do wideo.
Przygotowanie środowiska wykonuje te czynności w osobnym, **nienagrywanym**
kontekście i ponownie używa uzyskanej sesji przeglądarki, więc scenariusz docelowy
może być nagrany już zalogowany.

Scenariusz **docelowy** wskazuje scenariusz **setup** — zwykły, wcześniej
skompilowany `*.scenario.yaml`, zwykle taki, który uczy logowania — przez
`config.setup`, i pomija własne kroki logowania:

```yaml
# target.scenario.yaml
config:
  title: "Przegląd pulpitu"
  baseUrl: https://example.com
  setup: teach-login.scenario.yaml   # ścieżka względem TEGO scenariusza
steps:
  - navigate: /dashboard             # już zalogowani — tu żadnych kroków logowania
  - teach: "Otwórz sekcję Raporty"
```

Scenariusz setup deklaruje health-check we **własnym** `config`:

```yaml
# teach-login.scenario.yaml
config:
  baseUrl: https://example.com
  verifyUserLoggedIn: "Wyloguj"      # skrót na {containsText: "Wyloguj"}
  # pełna forma (wymagane jest tylko containsText):
  # verifyUserLoggedIn:
  #   containsText: "Wyloguj"
  #   url: /dashboard                # opcjonalne; domyślnie baseUrl scenariusza docelowego
  #   timeout: 8                     # opcjonalne; sekundy, domyślnie 8
  maxAgeHours: 12                    # opcjonalny TTL buforowanej sesji
steps:
  - navigate: /login
  - enterText: { into: "pole adresu e-mail", text: "${DEMO_EMAIL}" }
  - enterText: { into: "pole hasła", text: "${DEMO_PASSWORD}" }
  - teach: "Kliknij przycisk Zaloguj"
```

`verifyUserLoggedIn` przyjmuje tekst (skrót na `containsText`) albo obiekt.
Dopasowanie to zwykły, **rozróżniający wielkość liter podciąg** widocznego
`body.innerText` strony. Wybierz tekst, który pojawia się **tylko po
zalogowaniu** — nazwa użytkownika to najpewniejszy wybór; wylogowana stopka
przypadkiem zawierająca to słowo dałaby fałszywy pozytyw, bo dopasowanie nie ma
granic słów. `maxAgeHours` to opcjonalny czas życia sesji. Gdy nie ustawisz
**ani** `verifyUserLoggedIn`, **ani** `maxAgeHours`, obecny cache jest ufany aż do
`--force`, a narzędzie wypisuje głośne ostrzeżenie.

**Najpierw skompiluj scenariusz setup.** `guidebot compile CEL` i
`guidebot render CEL` obie automatycznie ustanawiają lub ponownie używają sesji
przed własną pracą, gdy cel ma `config.setup` — w typowym przypadku bez osobnego
kroku. Ale scenariusz setup musi być wcześniej skompilowany
(`guidebot compile teach-login.scenario.yaml`); jeśli nie jest, compile, render
albo `setup` celu kończą się jawnym błędem z instrukcją skompilowania setupu.
Ustanowienie sesji **odtwarza zamrożone cele setupu i wykonuje zero wywołań LLM.**

Aby zbudować lub odświeżyć buforowaną sesję ręcznie:

```bash
guidebot setup teach-login.scenario.yaml [--headed] [--force] [--timeout 15] [-v]
```

Zwykły przebieg przy żywym cache używa go ponownie
(`session reused (already live)`); w przeciwnym razie odtwarza setup i buforuje
wynik (`session refreshed and cached`). `--force` zawsze odbudowuje. `--headed`
pokazuje przeglądarkę i — jeśli health-check automatycznego odtworzenia się nie
powiedzie — zatrzymuje się, byś dokończył logowanie ręcznie (MFA/captcha), zanim
zrobi snapshot.

**Bezpieczeństwo.** Cache to Playwrightowy `storage_state` pod
`.guidebot/sessions/<klucz>.json` — poświadczenie na okaziciela. Jest zapisywany
z prawami `0600` (katalog `0700`), a narzędzie samo tworzy
`.guidebot/sessions/.gitignore` (`*`), więc nigdy nie trafia do repozytorium.
Klucz cache zawiera skrót wartości poświadczeń `${ENV}`, więc zmiana użytkownika
logowania odświeża sesję automatycznie.

**Ograniczenia (v1).** Buforowane są tylko cookies i `localStorage`; sesja
trzymana w `sessionStorage` albo IndexedDB (część SPA OIDC/MSAL) nie da się
zbuforować — narzędzie wykrywa to i zgłasza. Setup i cel muszą mieć to samo
pochodzenie (host); reużycie między pochodzeniami to twardy błąd. Jedna,
niezależna od języka sesja jest współdzielona przez warianty zlokalizowanego
zestawu renderów; jeśli backend przypina język UI do sesji, zamrożone
zlokalizowane etykiety mogą się rozjechać.

### Dwa tryby wielojęzycznego wyniku

Guidebot obsługuje dwa celowo odmienne przepływy:

1. **Jeden film z wybieralnymi ścieżkami audio.** Zachowaj jeden kanoniczny przebieg
   strony w zwykłym scenariuszu, dodaj alternatywne `config.audioTracks` i wpis
   `translations` dla każdego alternatywnego języka w każdym kroku z narracją
   `say`/`teach`. Przeglądarka jest nagrywana raz, a końcowy MP4 zawiera jeden strumień
   wideo i po jednym oznaczonym językiem strumieniu audio. Najdłuższa narracja kroku
   wyznacza moment wspólnej akcji obrazu. Tłumaczenia dotyczą wyłącznie renderu i nie
   trafiają do Codexa.
2. **Osobny, w pełni zlokalizowany film dla każdego języka.** Utwórz kompletny
   scenariusz dla każdego locale i połącz je manifestem `kind: localized-render-set`,
   `version: 1`. Użyj `compile-set` i `render-set`, gdy różnią się adresy, UI, opisy
   targetów, narracja albo tempo. Każdy wariant otrzymuje własny sidecar, świeży kontekst
   przeglądarki, nagranie i jednościeżkowy MP4.

```bash
# Jeden wspólny obraz z wieloma wybieralnymi ścieżkami audio
uv run guidebot compile examples/multilingual-login.scenario.yaml
uv run guidebot render examples/multilingual-login.scenario.yaml \
  --out out/multilingual-login.mp4

# Niezależny obraz i narracja dla każdego języka
uv run guidebot compile-set examples/localized-login.render-set.yaml
uv run guidebot render-set examples/localized-login.render-set.yaml \
  --output-dir out/localized-login
```

Dla MP4 z wieloma ścieżkami wszystkie głosy używają obecnie jednego providera, a każdy
krok z narracją musi mieć dokładnie wymagane tłumaczenia; standardowe CLI akceptuje
tylko provider `edge`. Gdy `audioTracks` nie jest puste, każda ścieżka musi mieć unikalne
`trackLanguage` w ISO 639-2; pierwsza ścieżka `config.tts` jest jedyną domyślną. Zobacz
[Wielojęzyczne audio](docs/pl/multilingual-audio.md) oraz
[Zlokalizowane zestawy renderów](docs/pl/localized-render-sets.md).

### Pliki

| Plik | Właściciel | Commitować? |
|---|---|---:|
| `flow.scenario.yaml` | Ty | Tak |
| `flow.compiled.yaml` | Generowany przez `compile`; nie edytuj ręcznie | Tak |
| `flow.render-set.yaml` | Ty; grupuje kompletne zlokalizowane scenariusze | Tak |
| `out/flow.mp4` | Generowany przez `render` | Zwykle nie |
| `.guidebot/audio/` | Trwałe audio i metadane JSON TTS; usuwaj ręcznie | Nie |
| `.guidebot/sessions/` | Buforowane sesje logowania (poświadczenia na okaziciela); auto-gitignore, usuwaj ręcznie | Nie |
| `<katalog-wyjściowy>/.guidebot_video/<nazwa-wyjścia>/` | Trwałe pliki WebM, composite i WAV danego filmu; usuwaj ręcznie | Nie |

Bieżący schemat pliku compiled ma wersję 2. Starszy sidecar może zostać odczytany na
potrzeby migracji, ale kompilacja i odtwarzanie uznają go za nieaktualny i wymagają
ponownej kompilacji.

Po zmianie instrukcji celu, trasy, rodzaju komendy, wyrównania kroków lub konfiguracji
targetów doprowadź `guidebot compile` do sukcesu przed renderem. Zmiany ograniczone do
narracji istniejącego kroku, alternatywnych ścieżek/tłumaczeń, `cursor` albo `chrome`
dotyczą wyłącznie renderu. Przed syntezą i nagrywaniem render odrzuca sidecar z błędną
nazwą źródła, liczbą slotów, wersją kompilatora, rodzajem komendy/akcji, instrukcją,
fingerprintem wybranej konfiguracji, stanem oczekiwania, gotowością albo zamrożonym
literałem `teach`. Następnie sprawdza na żywo tożsamość zwykłych akcji
kliknięcia/hover/wpisania tekstu; warunkowy `wait` nadal pomija tę kontrolę. Trasy,
`baseUrl` i zewnętrzny drift aplikacji nie są w pełni fingerprintowane, dlatego po ich
zmianie albo błędzie cyklu popupu/tożsamości użyj `compile --force`.

`.guidebot/audio/` przechowuje pliki MP3 i metadane JSON zawierające tekst narracji.
Katalog roboczy filmu może zachować WebM strony głównej i popupu, composite MP4 oraz
pełnej długości pliki `bed-<język>.wav`. Traktuj oba katalogi jako potencjalnie
wrażliwe i usuwaj je ręcznie, gdy przestaną być potrzebne.

Pełna polska instrukcja obejmuje [szybki start](docs/pl/getting-started.md),
[pliki scenariusza](docs/pl/scenario-files.md), kompletną
[dokumentację YAML](docs/pl/scenario-reference.md), [CLI](docs/pl/cli-reference.md),
[wielojęzyczne audio](docs/pl/multilingual-audio.md),
[zlokalizowane zestawy renderów](docs/pl/localized-render-sets.md) oraz
[rozwiązywanie problemów](docs/pl/troubleshooting.md).

Obecne ograniczenia wersji beta obejmują tylko jeden automatycznie śledzony cykl życia
popupu, brak obsługi zawartości iframe niezależnie od pochodzenia, brak jawnego wyboru
lub przełączania nazwanych kart, brak polecenia nagrywania/odkrywania, brak auto-heal,
wyłącznie adapter Edge TTS w standardowym CLI oraz brak wyboru backendu reasonera.
Przed wdrożeniem przepływu produkcyjnego przeczytaj dokumentację.

## License / Licencja

[MIT](LICENSE) © 2026 Michał Pasternak

[uv]: https://docs.astral.sh/uv/
[Codex CLI]: https://github.com/openai/codex
