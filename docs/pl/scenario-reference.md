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
| `popup` | Nie | Sposób kompozycji okna pop-up w filmie (tylko render). |
| `typing` | Nie | Animacja wpisywania znak po znaku; wyłącznie podczas renderu. |
| `sound` | Nie | Opcjonalne wbudowane efekty dźwiękowe; wyłącznie podczas renderu. |
| `intro` | Nie | Opcjonalna plansza tytułowa na start filmu; wyłącznie podczas renderu. |

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

Wszystkie pola są opcjonalne i render-only. Kursor zaczyna teraz każdy render na
środku viewportu (wcześniej w lewym górnym rogu) — to stała zmiana kosmetyczna, bez
osobnego pola konfiguracji.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `width`, `height` | `34`, `46` | Wymiary strzałki kursora w px. |
| `color`, `outline`, `glow` | czerwony, biały, czerwony halo | Kolory CSS. |
| `easing` | `cubic-bezier(.45,.05,.25,1)` | Krzywa ruchu. |
| `speed` | `1.15` | Piksele na milisekundę. |
| `minDuration`, `maxDuration` | `320`, `1400` | Granice czasu ruchu w ms. |
| `settle` | `280` | Pauza po dotarciu do celu w ms. |
| `click` | wartości domyślne | Wygląd rippla po kliknięciu; patrz niżej. |

Dla większego, lepiej widocznego kursora przy większych viewportach zwiększ razem
`width` i `height`, np. do `46`/`62`.

#### `cursor.click`

Wygląd rippla po kliknięciu. Wartości domyślne odtwarzają dzisiejszy ripple bez
zmian, więc pominięcie `click` zachowuje dotychczasowy wygląd.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `color` | `rgba(37,99,235,.9)` | Kolor CSS pierścienia rippla. |
| `scale` | `3.25` | Docelowa skala pierścienia; musi być większa od `0`. |
| `flash` | `false` | Gdy `true`, dodaje krótki wypełniony okrąg pod pierścieniem dla mocniejszego błysku kliknięcia. |

### `chrome`

Pasek w stylu macOS jest **powłoką z iframe**, wyłącznie podczas renderu. Docelowa
strona renderuje się w `<iframe>` osadzonym **poniżej** paska, więc pasek nigdy nie
zasłania treści strony — to gwarancja strukturalna, a nie górny padding. Przy
włączonym chrome viewport układu strony to `width × (height − chrome.height)`.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `false` | Włącza pasek podczas renderu. |
| `showUrl` | `true` | Pokazuje pole adresu. Gdy `false`, wpisywanie URL-a i jego opóźnienie są wyłączone. |
| `typeOnNavigate` | `true` | Domyślnie animuje wpisanie URL-a przed `goto`. |
| `height` | `56` | Wysokość paska w px; musi być dodatnia. Zmniejsza viewport strony. |
| `barColor`, `textColor`, `radius` | `#f3f4f6`, `#374151`, `12` | Wygląd paska. |
| `showLock` | `true` | Pokazuje dekoracyjną kłódkę dla HTTPS. |
| `closeColor`, `minimizeColor`, `maximizeColor` | kolory macOS | Kolory dekoracyjnych kropek. |
| `interactOnNavigate` | `true` | W kroku nawigacji kursor podjeżdża do pola adresu, klika, pole dostaje wygląd „w fokusie", a potem URL jest wpisywany. |
| `charDelayMs` | `110` | Bazowe opóźnienie na znak przy wpisywaniu (ms). |
| `charJitterMs` | `55` | Losowy jitter dodawany do opóźnienia każdego znaku (ms). |
| `segmentPauseMs` | `180` | Pauza między segmentami URL-a (ms). |
| `preNavigatePauseMs` | `400` | Pauza po zakończeniu wpisywania, przed załadowaniem (ms). |
| `focusColor` | `#3b82f6` | Kolor akcentu pola „w fokusie" (CSS). |
| `showCaret` | `true` | Pokazuje migający kursor w polu podczas wpisywania. |

Większość pól chrome jest kosmetyczna i **poza** config hashem, więc ich zmiana nie
wymusza rekompilacji. Wyjątki to `enabled` i `height`: oba zmieniają viewport
kompilacji strony (iframe ma wysokość `height − chrome.height`), więc **wchodzą** do
config hasha — włączenie lub wyłączenie chrome albo zmiana jego wysokości wymusza
rekompilację. Pola wpisywania i interakcji (`interactOnNavigate`, `charDelayMs`,
`charJitterMs`, `segmentPauseMs`, `preNavigatePauseMs`, `focusColor`, `showCaret`) są
tylko wizualne (render) i pozostają poza hashem.

Aby wczytać dowolne strony w iframe, render usuwa nagłówek `X-Frame-Options` oraz
dyrektywę CSP `frame-ancestors` z odpowiedzi i blokuje service workery podczas
renderu. Strona, która przekierowuje na swoim adresie wejściowym, ładuje się pod tym
adresem wejściowym, a pole adresu pokazuje URL nawigowany, nie ten po przekierowaniu.
Pełny URL może trafić do filmu — wyłącz `showUrl` dla adresów zawierających sekret.
Compile nie wstrzykuje paska.

### `popup`

Opcjonalny obiekt `popup` steruje tym, jak okno pop-up (patrz sekcja `teach`) jest
komponowane w filmie. Jest tylko dla renderu: podobnie jak `cursor`, **żadne** jego
pole nie wchodzi do config hasha, więc jego zmiana nigdy nie wymaga rekompilacji.

```yaml
popup:
  transition: slide
  slideMs: 400
```

`transition` wybiera sposób pojawienia się pop-upu:

- `cut` — twarde cięcie do pełnoekranowego nagrania pop-upu (pierwotne zachowanie).
- `float` — pop-up to zaokrąglone pływające okno z cieniem nad **przyciemnioną**
  stroną główną, która wciąż jest widoczna w tle; pojawia się i znika przez fade.
- `slide` — pop-up wjeżdża jako **pełnoekranowe** okno (push-left: strona główna
  wychodzi w lewo, pop-up wchodzi z prawej), trzyma pełny ekran, a przy zamknięciu
  wyjeżdża.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `transition` | wyprowadzane z `floating` | `cut`, `float` lub `slide` (patrz wyżej). |
| `floating` | `true` | Przestarzały alias bool: `true` → `float`, `false` → `cut`. Jawne `transition` ma pierwszeństwo. |
| `scale` | `0.72` | `float`: rozmiar pływającego okna jako ułamek viewportu. |
| `cornerRadius` | `14` | `float`: promień zaokrąglenia okna w px. |
| `shadow` | `true` | `float`: rysuje cień. |
| `backdropDim` | `0.45` | `float`: krycie ciemnego tła nad stroną główną. |
| `backdropBlur` | `0` | `float`: promień rozmycia tła w px. |
| `openMs` | `320` | `float`: czas pojawienia się (fade-in) w ms. |
| `closeMs` | `240` | `float`: czas zniknięcia (fade-out) w ms. |
| `slideMs` | `400` | `slide`: czas wjazdu/wyjazdu w ms. |

Komponowane pop-upy (`float` i `slide`) renderują się **bez ozdób**: samo okno
pop-upu nie ma paska adresu — rysowana jest tylko ramka kompozytora.

!!! note "Znane ograniczenie"

    Pop-up używa rozmiaru, o który poprosiło wywołanie `window.open(...)`. Jeśli jest
    on mniejszy niż viewport filmu, oprawione lub pełnoekranowe okno pokazuje puste
    miejsce wokół treści pop-upu. Wymuszenie wypełnienia viewportu przez pop-up jest
    planowanym usprawnieniem.

### `typing`

Render-only animacja wpisywania znak po znaku. Compile zawsze wypełnia pole
natychmiast; animuje wyłącznie `render`.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `animate` | `false` | Gdy `true`, wpisuje tekst znak po znaku zamiast wklejać go od razu. |
| `speed` | `60` | Milisekundy **na znak** — opóźnienie; im więcej, tym wolniej. Nie mylić z `cursor.speed`, które jest tempem (px/ms) — to dwa różne pojęcia. |

Zostaw `animate: false` dla pól maskowanych, formatowanych lub z autouzupełnianiem,
gdzie animacja znak po znaku mogłaby zniekształcić finalną wartość.

### `sound`

Render-only, opcjonalne, wbudowane efekty dźwiękowe wmiksowane pod narrację na każdej
ścieżce językowej. Dźwięki są wbudowane w Guidebota — nie podajesz własnych plików.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `false` | Włącza podkład dźwiękowy. |
| `click` | `true` | Odtwarza cichy dźwięk kliknięcia przy każdym kliknięciu. |
| `keys` | `true` | Odtwarza cichy dźwięk klawisza przy każdym wpisywanym znaku. Słyszalny tylko, gdy `typing.animate` jest też `true`. |
| `volume` | `-12.0` | Tłumienie w dB podkładu dźwiękowego; musi być `0` lub mniej. |

### `intro`

Render-only, opcjonalna plansza tytułowa. Gdy włączona, otwiera film zamiast
dzisiejszej pustej, białej pierwszej klatki; wyłączona (domyślnie) zostawia identyczny
biały start.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `false` | Pokazuje planszę tytułową. |
| `subtitle` | brak | Opcjonalny podtytuł. |
| `notes` | brak | Opcjonalne dodatkowe notatki. |

Plansza powstaje z `config.title` oraz `intro.subtitle` i `intro.notes`.

## Reguła kroku

Krok ma najwyżej jedną komendę główną spośród `teach`, `navigate`, `click`, `hover`,
`enterText`, `wait` i `slide`. `say` może być jedyną treścią kroku albo towarzyszyć
jednej akcji. Pusty krok i dwie akcje główne są błędem.

Narracją domyślną jest `say`, a gdy go nie ma — `teach`. Same `click`, `hover`,
`enterText`, `navigate`, `wait` i `slide` nie są czytane — tekst planszy `slide` jest
wyświetlany, nie wypowiadany.

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

### `slide`

```yaml
- slide:
    title: "Logowanie do systemu"
    subtitle: "Krok po kroku"        # opcjonalne
    notes: "Materiał szkoleniowy"    # opcjonalne
    hold: 2.5                        # opcjonalne; sekundy trzymania planszy bez `say`
  say: "Zaczynamy."                  # opcjonalna narracja, ODDZIELNA od tekstu na planszy
```

Plansza pełnoekranowa pokazywana w dowolnym miejscu scenariusza, bez naruszania
strony pod spodem. Wymaga co najmniej jednego z: `title`, `subtitle`, `notes`.

Tekst na planszy jest **wyświetlany, nie czytany**; narrację dostarcza osobno `say`.
W filmie wielojęzycznym tekst planszy pozostaje jednojęzyczny (jeden wspólny obraz) —
tylko `say` (i jego `translations`) zmienia się między ścieżkami `audioTracks`.

Tempo: gdy jest `say`, planszę wyznacza długość narracji, a `hold` jest ignorowane;
bez `say` plansza trzyma się `hold` sekund (domyślnie `2.5`). Naznaczona narracją
plansza nie może więc pozostać dłużej, niż trwa jej narracja — żeby przytrzymać
planszę *po* wypowiedzi, dodaj drugą, cichą planszę `slide` (ten sam tekst, `hold`,
bez `say`).

Dodanie, usunięcie lub zmiana kolejności kroku `slide` zmienia liczbę kroków, więc
jako jedyny rodzaj kroku **wymaga `guidebot compile`**; render sprawdza liczbę
kroków przed startem i kończy się błędem przy nieaktualnym sidecarze.

### `expect`

Model przyjmuje pole `expect`, lecz compiler sam wyprowadza gotowość z obserwowanej
zmiany URL i nie traktuje źródłowej wartości jako stabilnego sterowania. Nie używaj
`expect` w scenariuszach; dla SPA dodaj jawny `wait`.

## Macierz przebudowy

| Zmiana | Wymaga `guidebot compile`? |
|---|---:|
| `cursor` (rozmiar, `click`, wyśrodkowany start) | Nie — render-only |
| `typing`, `sound`, `intro`, `chrome` | Nie — render-only |
| Istniejący tekst narracji `say`/`teach`, `translations` | Nie — render-only |
| Sama wartość `enterText.text` | Nie — render-only |
| Dodanie, usunięcie lub zmiana kolejności kroku `slide` | Tak |
| Instrukcja targetu kroku (zdanie `teach`, `click`/`hover`, `enterText.into`, `wait.until`/`state`) | Tak |
| Zmiana rodzaju komendy kroku | Tak |

Pełną listę, łącznie z `viewport`/`locale`/`tts.lang` i driftem aplikacji, znajdziesz
w [Plikach scenariusza](scenario-files.md#co-uniewaznia-sidecar).

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
