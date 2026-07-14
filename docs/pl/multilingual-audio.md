# Wiele ścieżek audio

Ten tryb nagrywa **jeden wspólny przebieg przeglądarki** i umieszcza w jednym MP4
kilka wybieralnych ścieżek narracji. Użyj go, gdy interfejs, URL-e i akcje są takie
same we wszystkich językach.

Jeżeli język strony zmienia etykiety, host, ścieżki albo przebieg, wybierz
[zlokalizowany render-set](localized-render-sets.md).

## Scenariusz

`config.tts` jest ścieżką kanoniczną, pierwszą i domyślną. `audioTracks` zawiera
alternatywne ścieżki:

```yaml
config:
  title: "Logowanie"
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

  - teach: "Kliknij przycisk Zaloguj"
    translations:
      en-US: "Click the Sign in button"
```

Kompletny przykład:
[examples/multilingual-login.scenario.yaml](https://github.com/iplweb/guidebot-recorder/blob/main/examples/multilingual-login.scenario.yaml).

## Kontrakt języków

- `lang`, np. `pl-PL`, jest kluczem TTS i `translations`.
- `trackLanguage`, np. `pol`, jest małym, zarejestrowanym kodem ISO 639-2 zapisanym
  w metadanych MP4.
- Każda ścieżka musi mieć unikalne `lang` i `trackLanguage`.
- Gdy istnieje `audioTracks`, `trackLanguage` jest wymagane również w domyślnym `tts`.
- `title` jest nazwą strumienia widoczną w odtwarzaczu; bez niego używane jest `lang`.
- Wszystkie ścieżki jednego renderu muszą deklarować tę samą nazwę providera.
- Standardowe CLI wymaga, aby każdy provider był dokładnie `edge`.

## Kontrakt tłumaczeń

Każdy krok z narracją — `say` albo `teach` bez `say` — musi mieć dokładnie jeden wpis
dla każdego `audioTracks[].lang`. Brak, dodatkowy język, tłumaczenie bez narracji albo
duplikat języka powodują błąd walidacji.

`say` ma pierwszeństwo nad `teach` jako narracja kanoniczna. Tłumaczenie `teach` jest
**wyłącznie tekstem alternatywnego audio**. Nie trafia do reasonera i nie zmienia
akcji. Polski `teach` w przykładzie nadal kompiluje polski target, nawet gdy angielska
narracja mówi „Sign in”.

Dlatego ten tryb wymaga jednego wspólnego UI. Zmiana samego `translations` nie wymaga
ponownej kompilacji.

## Kompilacja i render

```bash
uv run guidebot validate examples/multilingual-login.scenario.yaml
uv run guidebot compile examples/multilingual-login.scenario.yaml
uv run guidebot render examples/multilingual-login.scenario.yaml \
  --out out/multilingual-login.mp4
```

Compile rozwiązuje tylko kanoniczne instrukcje. `audioTracks`, `translations`,
`trackLanguage` i `title` są render-only.

Render:

1. sprawdza sidecar v2;
2. syntetyzuje wszystkie języki przed nagrywaniem;
3. nagrywa przeglądarkę tylko raz, łącznie z obsługiwanym popupem;
4. przy każdym kroku czeka na najdłuższą z jego narracji, dzięki czemu wspólna akcja
   pasuje do wszystkich ścieżek;
5. buduje pełnej długości WAV na język i multipleksuje strumienie do MP4.

Wynik zawiera jeden obraz H.264 oraz po jednym AAC-LC 48 kHz stereo na język. Pierwszy
strumień, z `config.tts`, jest jedynym domyślnym.

## Artefakty

Dla `out/multilingual-login.mp4` pozostają między innymi:

```text
out/.guidebot_video/multilingual-login/bed-pol.wav
out/.guidebot_video/multilingual-login/bed-eng.wav
.guidebot/audio/<hash>.mp3
.guidebot/audio/<hash>.json
```

Udany render atomowo zastępuje MP4 oraz kompletny zestaw `bed-*.wav` i usuwa stare
języki z tego zestawu. Błąd syntezy, nagrania lub muxu zachowuje poprzedni poprawny
master i WAV-y.

JSON cache zawiera tekst narracji, a WAV-y są pełnymi dubami o długości filmu. Mogą
być użyte w procesie publikacji wymagającym osobnych plików audio, ale należy je też
traktować jako trwałe artefakty mogące zawierać dane.

## Provider przez API

Standardowy `guidebot render` tworzy `EdgeTtsProvider` i odrzuca inne nazwy przed
Chromium. Własny kod może przekazać inny obiekt `TtsProvider` do `run_render`, lecz
wszystkie ścieżki nadal muszą mieć wspólną nazwę providera. Obecny adapter Edge używa
przy syntezie pola `voice`; `model` i `speed` wpływają na cache, ale są ignorowane
przez wywołanie Edge TTS.
