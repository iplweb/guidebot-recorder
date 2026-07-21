# Pliki scenariusza

## Zalecany układ

```text
projekt/
├── scenarios/
│   ├── login.scenario.yaml                 # źródło zwykłego lub wielo-audio filmu
│   ├── login.compiled.yaml                 # generuje compile
│   ├── login.render-set.yaml               # manifest osobnych filmów językowych
│   ├── login.pl-PL.scenario.yaml           # pełny wariant polski
│   ├── login.pl-PL.compiled.yaml
│   ├── login.en-US.scenario.yaml           # pełny wariant angielski
│   └── login.en-US.compiled.yaml
├── .guidebot/
│   └── audio/                              # cache MP3 + JSON z tekstem narracji
└── out/
    ├── login.mp4
    ├── localized/login.pl-PL.mp4
    ├── localized/login.en-US.mp4
    └── .guidebot_video/                    # nagrania i pełne WAV
```

| Plik lub katalog | Właściciel | Git |
|---|---|---|
| `*.scenario.yaml` | Autor lub zewnętrzny agent | Commituj. |
| `*.render-set.yaml` | Autor lub zewnętrzny agent | Commituj. |
| `*.compiled.yaml` | `compile` / `compile-set` | Przejrzyj i commituj; nie edytuj. |
| `out/*.mp4` | `render` / `render-set` | Zwykle nie commituj do repozytorium z kodem. |
| `.guidebot/audio/` | Renderer | Nie commituj; usuwaj ręcznie. |
| `<katalog-output>/.guidebot_video/` | Playwright i ffmpeg | Nie commituj; usuwaj ręcznie. |

Cache TTS zawiera MP3 i JSON z tekstem narracji. Katalog roboczy może zawierać WebM,
WAV i zapis strony. Oba pozostają po renderze i mogą zawierać dane wrażliwe.

## Nazewnictwo

Zwykły loader akceptuje `.scenario.yaml`, `.scenario.yml`, `.yaml` i `.yml`, ale
zalecane jest `<nazwa>.scenario.yaml`. Sidecar powstaje obok źródła:

| Źródło | Sidecar |
|---|---|
| `login.scenario.yaml` | `login.compiled.yaml` |
| `login.pl-PL.scenario.yaml` | `login.pl-PL.compiled.yaml` |

Dla zestawu używaj czytelnej konwencji:

```text
login.render-set.yaml
login.pl-PL.scenario.yaml
login.en-US.scenario.yaml
login.pl-PL.mp4
login.en-US.mp4
```

Sufiks manifestu `.render-set.yaml` jest zaleceniem. Manifest wymaga jednak, aby
wskazane źródła kończyły się małym `.scenario.yaml` lub `.scenario.yml`, a outputy
małym `.mp4`.

## Który zestaw plików utworzyć

### Jeden obraz, jedna narracja

Utwórz jeden `*.scenario.yaml`, a potem `compile` i `render`.

### Jeden obraz, wiele narracji

Utwórz jeden scenariusz. `config.tts` opisuje język domyślny, `audioTracks` języki
alternatywne, a każdy narracyjny krok ma komplet `translations`. Powstaje jeden MP4 z
wieloma ścieżkami. Zobacz [Wiele ścieżek audio](multilingual-audio.md).

### Osobny zlokalizowany obraz na język

Utwórz pełne źródło dla każdego języka i manifest:

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

Każdy wariant ma własne `locale`, `baseUrl`, `navigate`, targety, narrację i sidecar.
Szczegóły: [Zlokalizowane zestawy renderów](localized-render-sets.md).

## Plansze (`slide`) i kosmetyka render-only

`config.typing`, `config.sound`, `config.intro` oraz większy, wbudowany kursor
(`config.cursor.width`/`height`/`click`) to opcjonalne ustawienia wyłącznie renderu:
włączanie i wyłączanie nigdy nie wymaga kompilacji. Krok `slide` jest inny — to
zwykły krok, więc jego dodanie, usunięcie lub zmiana kolejności zmienia liczbę
kroków i wymaga `guidebot compile`.

```yaml
config:
  title: "Reset hasła"
  viewport: { width: 1280, height: 720 }
  tts: { provider: edge, voice: pl-PL-ZofiaNeural, lang: pl-PL }
  cursor: { width: 46, height: 62 }
  typing: { animate: true, speed: 45 }
  sound: { enabled: true }
  intro: { enabled: true, subtitle: "Jak zresetować hasło" }

steps:
  - slide:
      title: "Reset hasła"
      subtitle: "Krok po kroku"
    say: "W tym filmie pokażę, jak zresetować hasło."
  - navigate: /forgot-password
  - enterText:
      into: "pole adresu e-mail"
      text: "${DEMO_EMAIL}"
    say: "Wpisuję adres e-mail konta."
  - teach: "Kliknij przycisk Wyślij link resetujący"
  - slide:
      title: "Gotowe"
      hold: 3
```

Otwierająca plansza `slide` niesie narrację zamiast pustej strony, a zamykająca nie
ma `say`, więc po prostu trzyma się przez `hold` sekund po zakończeniu ostatniego
kroku z narracją.

## Cykl życia zwykłego scenariusza

```bash
uv run guidebot validate scenarios/login.scenario.yaml
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
uv run guidebot render scenarios/login.scenario.yaml --out out/login.mp4 -v
```

`compile` nie modyfikuje źródła. Sidecar jest zapisywany atomowo po krokach, więc
nieudany przebieg może pozostawić częściowy postęp. Renderuj dopiero po udanej
kompilacji.

Sidecar v2 zapisuje źródło, wyrównane akcje, pełne fingerprinty, targety, tożsamości,
`opens_popup` i ewentualny `input_text`. Render przed TTS sprawdza jego zgodność ze
źródłem; podczas akcji sprawdza też żywą tożsamość celu.

## Cykl życia zestawu

```bash
uv run guidebot compile-set scenarios/login.render-set.yaml
uv run guidebot render-set scenarios/login.render-set.yaml \
  --output-dir out/localized
```

Scenariusze są rozwiązywane względem katalogu manifestu, outputy względem
`--output-dir`. Warianty wykonują się w kolejności manifestu, każdy w świeżym
kontekście. `render-set` wymaga aktualnych sidecarów wszystkich wariantów przed
uruchomieniem przeglądarki.

## Zmienne i sekrety

`${NAZWA}` działa tylko w:

- tekstowym `navigate` i obiektowym `navigate.url`;
- `enterText.text`.

Nie działa w manifeście, `baseUrl`, narracji ani opisie targetu. `.env` nie jest
ładowany automatycznie, a `$${` oznacza literalne `${`. Wartości z ENV nie trafiają do
sidecara, lecz mogą pojawić się w filmie, nagraniu roboczym lub logach aplikacji.

`teach` → `type` zapisuje jawny literal w sidecarze. Używaj go tylko dla danych
niewrażliwych; sekrety zawsze przekazuj przez `enterText` i ENV.

## Co unieważnia sidecar

| Zmiana | Skutek |
|---|---|
| Instrukcja targetu, targetowy rodzaj komendy lub stan `wait` | Fingerprint wymaga compile. |
| `viewport`, `locale`, domyślne `tts.lang` | Zmienia config hash. |
| Nazwa źródła, liczba kroków, compiler v1 | Mocny preflight odrzuca sidecar. |
| Dodanie, usunięcie lub zmiana kolejności kroku `slide` | Zmienia liczbę kroków — wymaga compile. |
| `say`, `translations`, alternatywne audio, `cursor` (łącznie z `cursor.click`), `chrome`, `typing`, `sound`, `intro` | Render-only. |
| `config.selects.settleMs`, `maxVisibleOptions`, `openHoldMs` | Render-only; kosmetyczne dostrojenie nakładki. |
| `config.selects.mode` (globalnie) przełączone `shim`/`native`, albo `select.mode` na kroku | Zmienia config hash albo fingerprint kroku — wymaga compile. |
| DOM, dane, cookies lub zmieniony `navigate` | Użyj `compile --force`; szybkie sprawdzenie nie otwiera strony. |
