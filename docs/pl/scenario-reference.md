# YAML scenariusza

Schemat jest zamknięty: nieznane pola powodują błąd. Dokument źródłowy ma dokładnie
dwa wymagane pola najwyższego poziomu: `config` i `steps`.

## Przykład

```yaml
config:
  title: "Logowanie do systemu"
  baseUrl: https://staging.example.com
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts:
    provider: edge
    voice: pl-PL-ZofiaNeural
    lang: pl-PL
    trackLanguage: pol
    title: Polski
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true

steps:
  - navigate: /login
  - say: "Pokażę, jak się zalogować."
  - enterText:
      into: "pole adresu e-mail"
      text: "${DEMO_EMAIL}"
    say: "Wpisuję adres e-mail."
  - teach: "Kliknij przycisk Zaloguj"
  - wait: { until: "nagłówek pulpitu", state: visible, timeout: 10 }
```

## `config`

| Pole | Wymagane | Znaczenie |
|---|---:|---|
| `title` | Tak | Czytelny tytuł scenariusza. |
| `viewport` | Tak | `width` i `height` kontekstu compile/render oraz filmu. |
| `tts` | Tak | Domyślna narracja i pierwszy, domyślny strumień audio. |
| `baseUrl` | Nie | Baza względnych wartości `navigate`. |
| `locale` | Nie | Locale kontekstu Chromium w obu fazach i część config hash. |
| `audioTracks` | Nie | Alternatywne ścieżki narracji w tym samym MP4. |
| `cursor` | Nie | Wygląd i timing syntetycznego kursora. |
| `chrome` | Nie | Opcjonalny syntetyczny pasek, wyłącznie podczas renderu. |

### `baseUrl`

```yaml
baseUrl: https://staging.example.com/app/
steps:
  - navigate: login
```

Wynik to `https://staging.example.com/app/login`. Wartość zaczynająca się od `/`,
np. `/login`, resetuje ścieżkę do korzenia hosta. Pełny `http://` lub `https://` jest
używany bez zmian. ENV nie jest rozwijane w `baseUrl`.

### `locale`

Compile i render tworzą świeże konteksty Playwrighta z tym samym `locale` oraz
viewportem. Zmiana locale unieważnia targety. Aplikacja może nadal wybierać język na
podstawie hosta, URL-a, cookies albo konta, więc te elementy stanu również ustawiaj
deterministycznie.

### `tts` i `audioTracks`

Każdy wpis TTS ma ten sam kształt:

| Pole | Wymagane | Znaczenie |
|---|---:|---|
| `provider` | Tak | Standardowe CLI wymaga `edge`; API może wstrzyknąć inny wspólny provider. |
| `voice` | Tak | Głos przekazywany do Edge TTS. |
| `lang` | Tak | Klucz narracji/tłumaczeń i cache; domyślne `tts.lang` wchodzi do config hash. |
| `model` | Nie | Część cache; obecny adapter Edge ignoruje przy syntezie. |
| `speed` | Nie | Część cache; obecny adapter Edge ignoruje przy syntezie. |
| `trackLanguage` | Warunkowo | Mały kod ISO 639-2 w metadanych MP4, np. `pol`, `eng`, `deu`. |
| `title` | Nie | Nazwa strumienia audio; domyślnie `lang`. |

Przy co najmniej jednym `audioTracks` wszystkie ścieżki, łącznie z `tts`, muszą mieć
unikalne `lang` i unikalne, poprawne `trackLanguage`. Jeden render może używać tylko
jednej nazwy providera, a standardowe CLI odrzuca zbiór inny niż `{edge}` przed
uruchomieniem Chromium. `title` i `trackLanguage` są metadanymi MP4 i nie zmieniają
syntezy ani klucza cache.

Szczegóły: [Wiele ścieżek audio](multilingual-audio.md).

### `cursor`

Wszystkie pola są opcjonalne i render-only:

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `width`, `height` | `34`, `46` | Wymiary kursora w px. |
| `color`, `outline`, `glow` | czerwony, biały, czerwony halo | Kolory CSS. |
| `easing` | `cubic-bezier(.45,.05,.25,1)` | Krzywa ruchu. |
| `speed` | `1.15` | Piksele na milisekundę. |
| `minDuration`, `maxDuration` | `320`, `1400` | Granice czasu ruchu w ms. |
| `settle` | `280` | Pauza po dotarciu do celu w ms. |

### `chrome`

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `false` | Włącza pasek podczas renderu. |
| `showUrl` | `true` | Pokazuje pole adresu. |
| `typeOnNavigate` | `true` | Domyślnie animuje wpisanie URL-a przed `goto`. |
| `height` | `56` | Wysokość zajęta wewnątrz viewportu. |
| `barColor`, `textColor`, `radius` | `#f3f4f6`, `#374151`, `12` | Wygląd paska. |
| `showLock` | `true` | Pokazuje dekoracyjną kłódkę dla HTTPS. |
| `closeColor`, `minimizeColor`, `maximizeColor` | kolory macOS | Kolory dekoracyjnych kropek. |

Pasek nie jest natywnym UI Chromium. Może zmienić responsywny układ strony, a pełny
URL może trafić do filmu. Wyłącz `showUrl` dla adresów zawierających sekret. Compile
nie wstrzykuje paska.

## Reguła kroku

Krok ma najwyżej jedną komendę główną spośród `teach`, `navigate`, `click`, `hover`,
`enterText` i `wait`. `say` może być jedyną treścią kroku albo towarzyszyć jednej
akcji. Pusty krok i dwie akcje główne są błędem.

Narracją domyślną jest `say`, a gdy go nie ma — `teach`. Same `click`, `hover`,
`enterText`, `navigate` i `wait` nie są czytane.

### `say`

```yaml
- say: "Otworzyliśmy panel użytkownika."
```

Nie wymaga targetu ani AI. Zmiana istniejącego tekstu jest render-only.

### `teach`

```yaml
- teach: "Kliknij przycisk Zapisz"
- teach: "Wpisz demo@example.com w pole E-mail"
```

Reasoner wybiera jedną akcję i target. Dla `type` musi zwrócić dokładny, niepusty
`inputText` będący literalnym fragmentem instrukcji; wartość jest sprawdzana i
zamrażana w sidecarze v2. Hasła, tokeny, kody i pola wyglądające na wrażliwe są
odrzucane — używaj wtedy `enterText` z ENV.

Jeżeli kliknięcie otworzy popup, Guidebot wykrywa go automatycznie. Nie dodawaj
komendy przełączania okna.

### `click` i `hover`

```yaml
- click: "przycisk Zapisz"
  say: "Zapisuję zmiany."
- hover: "menu Raporty"
```

Rodzaj akcji jest stały, a reasoner rozwiązuje tylko semantyczny target.

### `enterText`

```yaml
- enterText:
    into: "pole hasła"
    text: "${DEMO_PASSWORD}"
```

Do reasonera trafia `into`, nie `text`. Playwright używa `fill`, czyli zastępuje
bieżącą wartość. Guidebot nie maskuje pola w filmie ani logach aplikacji.

### `navigate`

```yaml
- navigate: /login
- navigate: { url: /login, type: true }
- navigate: { url: /login, type: false }
```

`type` nadpisuje animację paska tylko dla kroku; nie zmienia nawigacji i nie jest
wysyłane do reasonera.

### `wait`

```yaml
- wait: 1.5
- wait: { until: "tabela wyników", state: visible, timeout: 10 }
```

Liczba oznacza sekundy. Obiekt wymaga targetu; `state` przyjmuje `visible`, `hidden`
lub `enabled`, a timeout jest w sekundach i domyślnie wynosi 10. `hidden` może nie mieć
tożsamości. Obecne `enabled` czeka na widoczność, nie sprawdza osobno aktywności — nie
traktuj go jeszcze jako ścisłej bramki.

### `expect`

Model przyjmuje pole `expect`, lecz compiler sam wyprowadza gotowość z obserwowanej
zmiany URL i nie traktuje źródłowej wartości jako stabilnego sterowania. Nie używaj
`expect` w scenariuszach; dla SPA dodaj jawny `wait`.

## `translations`

`translations` jest dozwolone tylko na kroku z narracją i musi zawierać dokładnie po
jednym tekście dla każdego `audioTracks[].lang` — bez braków i dodatkowych kluczy:

```yaml
- teach: "Kliknij Zaloguj"
  translations:
    en-US: "Click Sign in"
```

Tłumaczenie zmienia wyłącznie alternatywne audio. Canonical `teach` nadal steruje
kompilacją i akcją.

## Podstawianie ENV

`${NAZWA}` jest rozwijane tylko w tekstowym `navigate`, `navigate.url` oraz
`enterText.text`. Brak zmiennej jest błędem; `$${` zapisuje literalne `${`. Guidebot
nie ładuje `.env` samodzielnie.

## Manifest zestawu

Manifest `localized-render-set` ma inny schemat niż scenariusz. Opisuje go strona
[Zlokalizowane zestawy renderów](localized-render-sets.md). Nie przekazuj manifestu do
zwykłego `guidebot validate`.
