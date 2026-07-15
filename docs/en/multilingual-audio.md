# Multilingual audio

Use multilingual audio when every viewer should see the **same browser recording**
but may select a different narration track. If the page, route, labels, or actions
also change by language, use [localized render sets](localized-render-sets.md).

## Configure the tracks

`config.tts` is the canonical and default track. Add alternate tracks under
`config.audioTracks`:

```yaml
config:
  title: "Signing in"
  baseUrl: https://example.com
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
  - say: "Witaj. Pokażę, jak się zalogować."
    translations:
      en-US: "Welcome. I will show you how to sign in."
  - navigate: /login
  - teach: "Kliknij przycisk Zaloguj."
    translations:
      en-US: "Click the Sign in button."
  - enterText: { into: "pole adresu e-mail", text: "${DEMO_EMAIL}" }
    say: "Wpisuję adres e-mail."
    translations:
      en-US: "I enter the email address."
```

See the complete
[multilingual example](https://github.com/iplweb/guidebot-recorder/blob/main/examples/multilingual-login.scenario.yaml).

`lang` is the exact key used in `translations` and in the TTS cache. It may be a
BCP 47-style value such as `en-US`. `trackLanguage` is different: it is the
registered, lowercase, three-letter ISO 639-2 code written into MP4 metadata, such
as `eng`, `pol`, or `deu`.

When `audioTracks` is nonempty:

- every track, including `config.tts`, needs a unique `lang`;
- every track needs a unique, registered `trackLanguage`;
- `title` is optional and falls back to `lang` in stream metadata;
- the stock CLI requires `provider: edge` on every track.

For a single-track scenario, `trackLanguage` remains optional. If omitted, the MP4
stream and retained bed use `und` (undetermined).

## Translate every narrated step

The `translations` mapping contains alternate narration only. Its keys must match
`audioTracks[*].lang` exactly:

- every step narrated by `say` or `teach` needs every alternate key;
- do not add the default `config.tts.lang` key;
- unknown or missing keys fail validation;
- a step with no narration must not have `translations`;
- if `say` accompanies an action or `teach`, it is the canonical narration and the
  translations correspond to that `say` text.

Translations are never used to resolve or change browser actions. The canonical
`teach`, `click`, `hover`, `enterText.into`, and `wait.until` still control the one
shared flow. They are also not subject to `${ENV_VAR}` substitution.

## Compile once, render all tracks

```bash
export DEMO_EMAIL=user@example.com
uv run guidebot validate scenarios/login.scenario.yaml
uv run guidebot compile scenarios/login.scenario.yaml
uv run guidebot render scenarios/login.scenario.yaml \
  --out out/login.mp4
```

Adding or editing alternate tracks and translations is render-only. Changing the
canonical target instruction still requires compilation, and changing the default
`tts.lang`, viewport, or locale changes the target fingerprint.

Before Chromium recording starts, Guidebot synthesizes every missing segment for
every language. If one synthesis fails, recording does not start. At each narrated
step all tracks begin at the same timeline offset; the shared browser action waits
for the longest language. Shorter tracks contain silence until that action. One
slow translation can therefore lengthen the common video.

## Output and retained beds

For `--out out/login.mp4`, a successful render produces:

```text
out/
├── login.mp4
└── .guidebot_video/
    └── login/
        ├── bed-pol.wav
        ├── bed-eng.wav
        └── ... Playwright/ffmpeg work files
```

The MP4 contains one H.264 video stream and one AAC-LC, 48 kHz stereo stream per
language. `config.tts` is first and is the sole default stream. Each stream receives
language, title, and handler metadata. The WAV beds are full video length, 48 kHz
stereo PCM and are useful when a publishing service asks for separate dubbing files.

A successful rerender atomically replaces the master and complete bed set and
removes beds for languages no longer configured. If bed construction or muxing
fails, the previous master and beds remain. Other WebM/work files in the private
workspace are not automatically cleaned.

Inspect the finished streams with ffprobe:

```bash
ffprobe -v error -select_streams a \
  -show_entries stream=index,codec_name,sample_rate,channels:stream_tags=language,title:stream_disposition=default \
  -of json out/login.mp4
```

## TTS cache and provider limits

Narration segments remain under `.guidebot/audio/` as MP3 plus JSON metadata that
contains the spoken text. The cache key includes text, `provider`, `voice`, `lang`,
`model`, `speed`, and adapter/schema versions. MP4-only `title` and `trackLanguage`
do not trigger resynthesis.

The Edge adapter currently uses only `voice` for synthesis; `model` and `speed` are
accepted and cached but do not alter Edge output. The stock `render` command rejects
any provider other than `edge` before opening the recording browser. A custom Python
caller may inject another TTS provider, but every track in one render must still use
the same configured provider name.

## What this mode does not localize

All tracks share one `config.locale`, route, DOM, cursor timeline, and browser
recording. Audio tracks cannot translate page labels or change an action. Use a
[localized render set](localized-render-sets.md) when each language needs its own
site locale, URL, scenario steps, compiled sidecar, or video.
