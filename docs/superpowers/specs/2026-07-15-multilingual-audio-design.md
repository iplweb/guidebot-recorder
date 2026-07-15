# Multilingual audio tracks — design

**Date:** 2026-07-15

**Status:** implemented contract
**Extends:** `2026-07-14-guidebot-recorder-design.md` and
`2026-07-14-popup-multiwindow-design.md`

## Goal

Render the browser flow exactly once, synthesize narration in multiple languages,
and place every language as a separately selectable audio stream in one MP4. The
legacy scenario remains valid and still produces one video plus one default audio
stream.

This contract localizes audio only. It deliberately keeps one canonical browser
locale, URL flow, and set of action intents. When those visual or behavioral inputs
must differ by language, use complete scenarios grouped by the
[`2026-07-15-localized-render-set-design.md`](2026-07-15-localized-render-set-design.md)
contract instead.

## Scenario contract

`config.tts` remains the canonical/default track. `config.audioTracks` contains
alternate tracks in output order:

```yaml
config:
  title: Logowanie do systemu
  viewport: {width: 1376, height: 800}
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
  - say: Witaj. Pokażę, jak się zalogować.
    translations:
      en-US: Welcome. I will show you how to log in.
  - teach: Kliknij odnośnik Logowanie.
    translations:
      en-US: Click the Log in link.
```

Rules:

- `say` or `teach` is the narration of the default track.
- `translations` is render-only and is keyed by the alternate track's `lang`.
- Every narrated step must contain exactly one translation for every alternate
  track. Missing and unknown keys fail validation; there is no mixed-language
  fallback.
- A step without `say`/`teach` cannot carry `translations`.
- Every `lang` is unique. When `audioTracks` is non-empty, every track also has a
  unique lowercase three-letter `trackLanguage` (ISO 639-2).
- The default track is physically first in the MP4 and is the only stream with the
  `default` disposition.
- All tracks use one TTS provider in v1. Voices, languages, models, and speeds may
  differ. The shipped CLI has one registered implementation, Edge TTS, and rejects
  any provider name other than `edge` before opening the browser. Python API callers
  may inject a custom implementation for a shared non-Edge provider name.

`lang` serves TTS and translation lookup (normally BCP 47, such as `pl-PL`).
`trackLanguage` serves the MP4 container (such as `pol`). Keeping both explicit is
intentional: region-bearing BCP 47 tags are not reliably retained by MP4 muxers.

## Compile boundary

Translated narration must never change browser behavior. The compiler and
`compiledFrom` continue to read only the canonical `teach`; `translations` is never
sent to the Reasoner and never changes the action fingerprint.

The compile hash remains the projection of viewport, browser locale, and the
default `tts.lang`. Alternate tracks, voices, titles, `trackLanguage`, and translated
text are render-only. Adding or editing dubbing therefore does not require a new
compile when the canonical action intent is unchanged.

Environment expansion remains forbidden in all narration, including
`translations`, so secrets cannot enter TTS, its cache key, or generated audio.

## Render and timing

Phase 0 synthesizes every `(narrated step, audio track)` pair before opening the
recorded browser. Failure of any language aborts before video recording.

For each narrated step:

```text
shared offset = current video-clock offset
place every language segment at shared offset
step narration duration = max(duration of every language segment)
wait(step narration duration)
perform the browser action
```

Shorter tracks contain silence until the shared action. This preserves one visual
timeline without overlapping a long translation with the next action or speech.
Adding a longer translation can lengthen the finished film; it never time-stretches
or truncates speech silently.

Render forces a short final video frame after the last action/narration. Assembly
then rejects any narration end that still exceeds the probed picture duration; even
a sub-frame overrun is an error rather than an allowed audio trim.

After recording, Guidebot builds one 48 kHz stereo, full-duration WAV bed per track.
The durable work files are namespaced per film and named
`<output-dir>/.guidebot_video/<output-stem>/bed-<trackLanguage>.wav`. They are also
suitable as audio-only language uploads where the target service asks for them.
Beds are staged as a complete set and published only after a successful mux; a
successful rerender removes `bed-*.wav` files for languages no longer configured.

Pop-up composition is unchanged and runs once. Its resulting H.264 picture receives
the same set of audio tracks as the ordinary single-page render.

## MP4 mux contract

The final container contains:

- exactly one video stream;
- one AAC-LC, 48 kHz stereo stream per configured language;
- audio streams ordered as default `tts`, then `audioTracks` order;
- ISO 639-2 `language` metadata;
- a readable `handler_name`/title;
- exactly one default audio disposition;
- the `moov` atom moved to the front (`faststart`).

The video duration is authoritative. All WAV beds must match it before muxing, and
the multi-track mux uses an explicit video duration rather than global `-shortest`;
one malformed short language track must not truncate the entire film. The ordinary
WebM path encodes the picture once to H.264. The already encoded pop-up composite is
stream-copied.

## YouTube note

The embedded multi-track MP4 is the portable master and works in players that expose
alternate audio streams. YouTube's documented Studio workflow still asks creators to
select an audio-only file for each added language, roughly matching the video length;
it does not promise to import every embedded MP4 stream automatically. Use the
generated `bed-<trackLanguage>.wav` files for that workflow. See
[YouTube multi-language audio](https://support.google.com/youtube/answer/13338784?hl=en)
and [recommended upload encoding](https://support.google.com/youtube/answer/1722171?hl=en-GB).

## Acceptance gates

- A legacy scenario produces one video and one default audio stream.
- A two-language scenario produces one video and two distinct audio streams with
  correct language, handler name, order, and dispositions.
- Both ordinary and pre-encoded/pop-up mux paths support multiple tracks.
- Missing/unknown translations, duplicate languages, invalid ISO codes, multiple
  providers, and mismatched bed durations fail loudly.
- TTS cache entries remain isolated by synthesis settings and text; MP4-only metadata
  does not invalidate audio cache entries.
- Browser pacing uses the longest language at each step.
- Alternate narration changes neither `configHash` nor canonical `compiledFrom`.

## Self-review decisions

- A mapping that silently falls back to canonical text was rejected because it can
  create a supposedly English track containing Polish fragments.
- Pacing by the default language was rejected because longer translations overlap
  later actions. Pacing by per-step maximum is slower for short languages but keeps
  all tracks semantically correct.
- BCP 47 was not copied directly into MP4 metadata; explicit ISO 639-2 avoids tags
  that appear in FFmpeg logs but disappear from the written container.
- Global `-shortest` was rejected for the multi-track mux because the shortest audio
  input could shorten the video. Full-duration beds plus an authoritative video
  duration make failure observable.
- Re-rendering the browser once per language was rejected for an embedded multi-audio
  master because it can produce visually different runs and cannot yield one shared
  picture timeline. Separate localized films intentionally make the opposite
  trade-off and are specified by `2026-07-15-localized-render-set-design.md`.
