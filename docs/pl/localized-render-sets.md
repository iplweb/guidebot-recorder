# Zlokalizowane zestawy renderów

Render-set grupuje pełne scenariusze językowe i tworzy **osobny, jednościeżkowy MP4
dla każdego wariantu**. Użyj go, gdy język zmienia stronę, host, ścieżkę, etykiety,
instrukcje akcji albo naturalne tempo filmu.

Jeżeli zmienia się tylko narracja tego samego UI, prostszy jest
[jeden MP4 z wieloma ścieżkami audio](multilingual-audio.md).

## Pliki

```text
scenarios/
├── login.render-set.yaml
├── login.pl-PL.scenario.yaml
├── login.pl-PL.compiled.yaml
├── login.en-US.scenario.yaml
└── login.en-US.compiled.yaml
```

Każdy scenariusz jest kompletny. Wariant angielski zawiera angielskie `teach`,
`click`, `hover`, `wait.until`, `enterText.into`, `select.from`/`option` i
`highlight.what`; nie są one pobierane z `translations` ani automatycznie
tłumaczone.

Przykłady:

- [manifest](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.render-set.yaml);
- [wariant polski](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.pl-PL.scenario.yaml);
- [wariant angielski](https://github.com/iplweb/guidebot-recorder/blob/main/examples/localized-login.en-US.scenario.yaml).

## Manifest

```yaml
kind: localized-render-set
version: 1
variants:
  pl-PL:
    scenario: login.pl-PL.scenario.yaml
    output: login.pl-PL.mp4
  en-US:
    scenario: login.en-US.scenario.yaml
    output: login.en-US.mp4
```

Reguły schematu:

- `kind` ma dokładnie wartość `localized-render-set`, a `version` jest całkowitym `1`;
- `variants` jest niepustą mapą, a jej kolejność jest kolejnością wykonania;
- klucz ma kanoniczny kształt BCP 47 obsługiwany w v1: 2–3 małe litery języka,
  opcjonalny script, region i warianty, np. `pl-PL`, `en-US`, `zh-Hans-CN`;
- wpis ma dokładnie pola `scenario` i `output`; nieznane pola są błędem;
- `scenario` jest względną ścieżką kończącą się `.scenario.yaml` lub
  `.scenario.yml`, rozwiązywaną względem katalogu manifestu;
- `output` jest względną ścieżką z małym `.mp4`, rozwiązywaną pod wymaganym
  `--output-dir`;
- ścieżki używają `/`; absolutne ścieżki, dyski Windows, dwukropki, backslashe i `..`
  są odrzucane;
- symlink nie może wyprowadzić scenariusza poza katalog manifestu ani outputu poza
  `--output-dir`;
- scenariusze, ich wynikowe sidecary i outputy muszą być unikalne także po
  normalizacji wielkości liter;
- MP4 nie może pokrywać się z prywatnym `.guidebot_video/<stem>` innego wariantu.

Nazwa `*.render-set.yaml` jest zalecaną konwencją, nie walidowanym sufiksem. Manifest
nie rozwija `${ENV}`.

## Kontrakt wariantu

Każdy wskazany scenariusz zachowuje zwykłą składnię, ale dodatkowo:

- `config.locale` musi być równe kluczowi wariantu;
- `config.tts.lang` musi być równe kluczowi wariantu;
- `config.tts.trackLanguage` jest wymaganym kodem ISO 639-2, np. `pol` lub `eng`;
- `config.audioTracks` musi być nieobecne albo puste;
- wszystkie warianty muszą deklarować tę samą nazwę providera;
- standardowy `render-set` wymaga providera `edge`.

`locale` jest stosowane w świeżym kontekście zarówno podczas compile, jak i renderu.
Wariant może mieć własny `title`, `baseUrl`, viewport, głos, pasek, kursor, kroki i
liczbę kroków. Dla spójnej publikacji warto zachować wspólną rozdzielczość.

`trackLanguage` opisuje strumień MP4 kodem ISO 639-2 (`pol`), natomiast klucz
wariantu/locale/TTS używa BCP 47 (`pl-PL`). To dwa różne pola.

Ponieważ `audioTracks` jest puste, wariant nie używa `translations`. Jego kanoniczne
instrukcje są kompilowane bezpośrednio przeciw odpowiednio zlokalizowanej stronie.

## Kompilacja

```bash
export DEMO_EMAIL=user@example.com
uv run guidebot compile-set scenarios/login.render-set.yaml
```

`compile-set` najpierw wczytuje cały manifest i wszystkie scenariusze. Błąd późnego
wariantu zatrzymuje proces jeszcze przed Chromium. Potem warianty są odwiedzane
sekwencyjnie, każdy w świeżym kontekście z własnym locale.

Aktualne sidecary v2 są ponownie używane. `--force` kompiluje wszystkie warianty od
nowa. Standardowe polecenie używa CodexReasoner; własny runner może przekazać inny
`Reasoner` do `run_compile_set`.

Sidecary nie są współdzielone między językami. Błąd zatrzymuje zestaw; wcześniejszy
postęp pozostaje, późniejsze warianty nie startują.

## Render

```bash
uv run guidebot render-set scenarios/login.render-set.yaml \
  --output-dir out/localized
```

Aliasem jest `--out-dir`. Przed TTS, Chromium i utworzeniem outputu Guidebot:

1. sprawdza wszystkie ścieżki, symlinki i kolizje workspace;
2. wymaga providera `edge` w standardowym CLI;
3. sprawdza aktualność sidecara każdego wariantu;
4. w razie braku wskazuje `guidebot compile-set` i nie uruchamia reasonera.

Dla przykładu powstają:

```text
out/localized/login.pl-PL.mp4
out/localized/login.en-US.mp4
out/localized/.guidebot_video/login.pl-PL/bed-pol.wav
out/localized/.guidebot_video/login.en-US/bed-eng.wav
```

Każdy MP4 zawiera jeden obraz H.264 i jeden domyślny AAC-LC 48 kHz stereo z językiem
`trackLanguage`. Wspólny cache segmentów pozostaje w `.guidebot/audio/` względem
katalogu uruchomienia.

## Izolacja i błędy

Warianty mogą współdzielić proces Chromium, ale nie kontekst. Cookies, local/session
storage, service worker, uprawnienia, otwarte strony i zalogowana sesja nie przechodzą
między językami. Każdy pełny scenariusz musi sam przygotować potrzebny stan.

Zestaw nie jest jedną transakcją:

- warianty kończą się w kolejności manifestu;
- pierwszy błąd zatrzymuje dalsze wykonanie;
- gotowe wcześniejsze MP4 pozostają poprawne;
- nieudany wariant nie zastępuje swojego poprzedniego MP4 ani WAV częściowym wynikiem;
- późniejsze warianty nie startują;
- istniejący MP4 nie powoduje pominięcia renderu — żądane warianty renderują się
  ponownie.

Set-level komunikaty redagują rozwinięte wartości `enterText.text` i `navigate`, ale
wartości nadal mogą być widoczne w nagraniu lub logach aplikacji.

## Ograniczenia v1

- Brak `validate-set`, filtrowania pojedynczego wariantu i równoległego wykonania.
- Brak dziedziczenia wspólnego scenariusza, overlayu akcji i automatycznego tłumaczenia.
- Dokładnie jedna ścieżka audio na wariant i jeden provider w całym zestawie.
- Brak transakcji obejmującej wszystkie warianty.
- Zwykłe ograniczenia scenariusza nadal obowiązują, w tym najwyżej jeden popup oraz
  brak obsługi iframe.
