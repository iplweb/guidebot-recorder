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
| `setup` | Nie | Na **celu**: ścieżka do scenariusza setup, którego buforowana sesja jest ustanawiana przed compile i render. Wchodzi do config hasha celu. |
| `verifyUserLoggedIn` | Nie | Na **setupie**: health-check logowania buforowanej sesji. Render-only (poza config hashem setupu). |
| `maxAgeHours` | Nie | Na **setupie**: opcjonalny TTL buforowanej sesji. Render-only (poza config hashem setupu). |
| `audioTracks` | Nie | Alternatywne ścieżki narracji w tym samym MP4. |
| `cursor` | Nie | Wygląd i timing syntetycznego kursora. |
| `chrome` | Nie | Opcjonalny syntetyczny pasek, wyłącznie podczas renderu. |
| `popup` | Nie | Sposób kompozycji okna pop-up w filmie (tylko render). |
| `typing` | Nie | Animacja wpisywania znak po znaku; wyłącznie podczas renderu. |
| `sound` | Nie | Opcjonalne wbudowane efekty dźwiękowe; wyłącznie podczas renderu. |
| `intro` | Nie | Opcjonalna plansza tytułowa na start filmu; wyłącznie podczas renderu. |
| `holdFrameForNarration` | Nie | Zamraża obraz na czas narracji zamiast nagrywać w czasie rzeczywistym; wyłącznie podczas renderu. |
| `holdFrameSettle` | Nie | Sekundy realnego czasu nagrane przed zamrożeniem klatki; wyłącznie podczas renderu. |
| `selects` | Nie | Nakładka DOM zamieniająca natywny `<select>` na widżet, którego lista jest widoczna na filmie; do config hasha wchodzi tylko `mode`. |

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

### `setup`, `verifyUserLoggedIn` i `maxAgeHours`

Te trzy pola realizują **przygotowanie środowiska przed nagraniem**: nagranie
scenariusza docelowego z serwisem już przygotowanym (zalogowanym, z
zaakceptowanymi cookies) bez pokazywania tego przygotowania na filmie.
Przygotowanie działa w osobnym, nienagrywanym kontekście przeglądarki, a uzyskana
sesja (Playwrightowy `storage_state`) jest buforowana i używana ponownie.

**`setup` — w scenariuszu docelowym.** Ścieżka, względem pliku docelowego, do
scenariusza setup — zwykłego, wcześniej skompilowanego `*.scenario.yaml`, zwykle
takiego, który uczy logowania. Cel pomija wtedy własne kroki logowania.

```yaml
config:
  setup: teach-login.scenario.yaml
```

`guidebot compile` i `guidebot render` celu automatycznie ustanawiają lub
ponownie używają buforowanej sesji przed własną pracą, gdy `setup` jest
ustawione. **Scenariusz setup musi być najpierw skompilowany**
(`guidebot compile teach-login.scenario.yaml`); inaczej compile, render albo
`guidebot setup` celu kończą się jawnym błędem z instrukcją skompilowania setupu.
Ustanowienie sesji odtwarza zamrożone cele setupu i wykonuje **zero wywołań
LLM**. Scenariusz użyty jako źródło setupu nie może sam deklarować `config.setup`
(rekurencja to błąd walidacji), a setup i cel muszą mieć to samo pochodzenie
(host); reużycie między pochodzeniami to twardy błąd.

Ponieważ `setup` zmienia zalogowany DOM, względem którego compile rozwiązuje
cele, **nie jest kosmetyczne**: ścieżka `setup` wchodzi do config hasha celu
(patrz [macierz przebudowy](#macierz-przebudowy)), więc dodanie, usunięcie lub
przepięcie `setup` rozwiązuje cele od nowa. (Sama zmiana użytkownika logowania
odświeża buforowaną sesję, ale nie przebudowuje automatycznie sidecara celu —
kontrola tożsamości przy renderze wychwyci ewentualny dryf DOM głośnym błędem.)

**`verifyUserLoggedIn` — w scenariuszu setup.** Health-check logowania, który
decyduje, czy buforowana sesja jest wciąż ważna. Przyjmuje tekst (skrót na
`containsText`) albo obiekt:

```yaml
config:
  verifyUserLoggedIn: "Wyloguj"      # skrót na {containsText: "Wyloguj"}
  # pełna forma:
  # verifyUserLoggedIn:
  #   containsText: "Wyloguj"        # wymagane
  #   url: /dashboard                # opcjonalne; domyślnie baseUrl celu
  #   timeout: 8                     # opcjonalne; sekundy, domyślnie 8
```

| Pole | Wymagane | Domyślnie | Znaczenie |
|---|---:|---:|---|
| `containsText` | Tak | — | Tekst, który musi być na stronie, by sesja liczyła się jako żywa. |
| `url` | Nie | `baseUrl` celu | Strona odwiedzana przed sprawdzeniem. Cookies są związane z pochodzeniem, więc sprawdzenie idzie na pochodzenie celu. |
| `timeout` | Nie | `8` | Sekundy odpytywania o `containsText`, zanim uznamy sesję za wylogowaną. |

Dopasowanie to zwykły, **rozróżniający wielkość liter podciąg** wyrenderowanego
`document.body.innerText` strony. Wybierz tekst, który pojawia się **tylko po
zalogowaniu** — nazwa użytkownika to najpewniejszy wybór; ponieważ dopasowanie
nie ma granic słów, wylogowana stopka typu `wyloguj się kiedy chcesz` dałaby
fałszywy pozytyw.

**`maxAgeHours` — w scenariuszu setup.** Opcjonalny czas życia buforowanej sesji,
liczony z `created_at` cache (nie z mtime pliku, więc przeżywa `git clean`, kopie
i restore w CI). Po przekroczeniu wieku sesja jest odświeżana przy kolejnym
compile/render/`setup`.

Gdy scenariusz setup nie deklaruje **ani** `verifyUserLoggedIn`, **ani**
`maxAgeHours`, obecny cache jest ufany aż do `--force`, a narzędzie wypisuje
głośne ostrzeżenie. Gwarancja „nigdy po cichu wylogowany" obowiązuje tylko, gdy
health-check jest skonfigurowany.

Zarówno `verifyUserLoggedIn`, jak i `maxAgeHours` są **render-only** na pliku
setup: pozostają poza config hashem samego setupu. Budowanie lub odświeżanie
cache ręcznie opisuje [`guidebot setup`](cli-reference.md#guidebot-setup).

!!! warning "Znane ograniczenia (v1)"

    Buforowane są tylko cookies i `localStorage`; sesja trzymana w
    `sessionStorage` albo IndexedDB (część SPA OIDC/MSAL) nie da się zbuforować —
    narzędzie wykrywa to i zgłasza. Setup i cel muszą mieć to samo pochodzenie.
    Jedna, niezależna od języka sesja jest współdzielona przez warianty
    zlokalizowanego zestawu renderów; jeśli backend przypina język UI do sesji,
    zamrożone zlokalizowane etykiety mogą się rozjechać.

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
| `easing` | `cubic-bezier(.45,.05,.25,1)` | Krzywa ruchu (`cubic-bezier(...)`, liczona w JS). |
| `bow` | `0.12` | Głębokość łuku, po którym porusza się kursor, jako ułamek dystansu. `0` = ruch po prostej. |
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
| `charDelayMs` | `60` | Bazowe opóźnienie na znak przy wpisywaniu (ms). |
| `charJitterMs` | `55` | Pasmo jittera (ms) wokół opóźnienia znaku. Losowanie jest skośne w prawo (log-normalne): większość znaków trafia blisko `charDelayMs`, mniejszość jest wyraźnie wolniejsza, a żaden nie jest szybszy niż `charDelayMs − charJitterMs`. |
| `segmentPauseMs` | `180` | Pauza między segmentami URL-a (ms). Wypada tylko na *prawdziwej* granicy — zdublowany separator, np. drugi `/` w `://`, jest pisany jednym ruchem i nie dostaje pauzy. |
| `maxDelayFactor` | `2.5` | Twardy sufit opóźnienia pojedynczego znaku, jako wielokrotność `charDelayMs`. Sporadyczna pauza „na zastanowienie" nigdy nie sumuje się z pauzą segmentową, więc żaden znak nie zawiesza się absurdalnie długo. |
| `preNavigatePauseMs` | `400` | Pauza po zakończeniu wpisywania, przed załadowaniem (ms). |
| `focusColor` | `#3b82f6` | Kolor akcentu pola „w fokusie" (CSS). |
| `showCaret` | `true` | Pokazuje migający kursor w polu podczas wpisywania. |

Większość pól chrome jest kosmetyczna i **poza** config hashem, więc ich zmiana nie
wymusza rekompilacji. Wyjątki to `enabled` i `height`: oba zmieniają viewport
kompilacji strony (iframe ma wysokość `height − chrome.height`), więc **wchodzą** do
config hasha — włączenie lub wyłączenie chrome albo zmiana jego wysokości wymusza
rekompilację. Pola wpisywania i interakcji (`interactOnNavigate`, `charDelayMs`,
`charJitterMs`, `segmentPauseMs`, `maxDelayFactor`, `preNavigatePauseMs`,
`focusColor`, `showCaret`) są
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
| `scale` | `0.85` | `float`: rozmiar pływającego okna jako ułamek viewportu. |
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

Render-only animacja wpisywania znak po znaku, **domyślnie włączona**. Pola
formularza wpisują się z tym samym naturalnym odczuciem co pasek adresu — bazowe
opóźnienie na znak plus jitter. Compile zawsze wypełnia pole natychmiast; animuje
wyłącznie `render`.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `animate` | `true` | Wpisuje tekst znak po znaku zamiast wklejać od razu. Ustaw `false` per scenariusz, by wrócić do natychmiastowego wypełnienia. |
| `speed` | `60` | Bazowe milisekundy **na znak** — opóźnienie; im więcej, tym wolniej. Nie mylić z `cursor.speed`, które jest tempem (px/ms) — to dwa różne pojęcia. |
| `jitterMs` | `40` | Pasmo jittera (ms) wokół `speed`, żeby wpisywanie było naturalne, nie metronomiczne. Skośne w prawo jak w pasku adresu: głównie blisko `speed`, sporadycznie wolniej, nigdy poniżej `speed − jitterMs`; zdublowany znak zachowuje tylko piątą część pasma. |
| `maxDelayFactor` | `2.5` | Twardy sufit opóźnienia pojedynczego znaku, jako wielokrotność `speed`. |

Ustaw `animate: false` dla pól maskowanych, formatowanych lub z autouzupełnianiem,
gdzie animacja znak po znaku mogłaby zniekształcić finalną wartość (finalna wartość
i tak jest korygowana).

### `sound`

Render-only, wbudowane efekty dźwiękowe wmiksowane pod narrację na każdej ścieżce
językowej, **domyślnie włączone**. Dźwięki są wbudowane w Guidebota — nie podajesz
własnych plików.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `true` | Podkład dźwiękowy. Ustaw `false` dla cichego filmu (sama narracja). |
| `click` | `true` | Cichy dźwięk kliknięcia przy każdym kliknięciu (oraz kliknięciu pigułki paska adresu). |
| `keys` | `true` | Cichy dźwięk klawisza na każdy wpisywany znak — zarówno w polach formularza (gdy `typing.animate`), jak i podczas wpisywania w **pasek adresu**. |
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

### `fade`

Render-only, płynne wejście i wyjście gotowego filmu. Wyłączone domyślnie: włączenie
wymusza przekodowanie obrazu w finalnym muksie (filtra nie da się nałożyć na
kopiowany strumień), więc scenariusz, który o fade nie prosi, daje bajt w bajt to
samo wyjście co dotąd.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `enabled` | `false` | Włącza fade. |
| `in` | `0.6` | Sekundy wejścia z koloru na starcie. |
| `out` | `0.8` | Sekundy wyjścia w kolor na końcu. |
| `color` | `black` | Kolor, z/do którego przechodzi obraz (nazwa lub `0xRRGGBB`). |
| `audio` | `true` | Ścisza też lektora równolegle z obrazem. |

`in` i `out` mogą być zerem — wtedy dana strona filmu nie ma przejścia. Suma obu nie
może przekroczyć długości filmu. Nie wchodzi do `config_hash`, więc włączenie
lub zmiana fade nie wymaga ponownego `compile`.

```yaml
config:
  fade:
    enabled: true
    in: 0.6
    out: 1.0
```

### `holdFrameForNarration` i `holdFrameSettle`

Sterowanie tempem renderu, wyłącznie podczas renderu, **domyślnie włączone**, i —
podobnie jak `cursor` i `popup` — poza config hashem.

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `holdFrameForNarration` | `true` | Zamiast trzymać przeglądarkę żywą przez cały czas trwania narracji kroku, render nagrywa tylko `holdFrameSettle` sekund, po czym zamraża tę klatkę; oddzielny przebieg ffmpeg dokleja zamrożoną klatkę na pozostały czas lektora. |
| `holdFrameSettle` | `1.0` | Sekundy realnego czasu wciąż nagrywane przed zamrożeniem klatki — dają czas animacjom wywołanym przez ten krok (np. rozwijaniu akordeonu, pojawianiu się treści), by dokończyć się pod głosem, tak jak przed tą funkcją. Settle jest opłacany *z* narracji, nie dodawany do niej, więc długość gotowego filmu się nie zmienia. Jeśli narracja kroku jest krótsza niż `holdFrameSettle`, cały krok nagrywa się w czasie rzeczywistym i zamrożenie nie następuje. Minimum to `2/25` s (dwie klatki przy 25 fps renderu): poniżej jednej klatki settle jest w ogóle nieprzedstawialny na siatce klatek renderu. Druga klatka to celowy margines ponad to jednoklatkowe minimum, nie coś, czego wymaga sam argument o nieprzedstawialności — wartość `1/25` s została sprawdzona i renderuje się poprawnie. |

Gotowy film ma **taką samą długość i tempo** niezależnie od tego, czy
`holdFrameForNarration` jest włączone — zmienia się tylko czas nagrywania. Może
jednak **inaczej wyglądać**: przy domyślnym ustawieniu strona stoi nieruchomo pod
lektorem tam, gdzie wcześniej wciąż się animowała. Ponowne wyrenderowanie istniejącego
scenariusza z tym domyślnym ustawieniem nie odtworzy pikseli filmu nagranego przed tą
funkcją — tylko jego długość i tempo. Użyj `guidebot render --no-hold-frame`, aby
nagrywać w pełni na żywo, jak dawniej; patrz [Dokumentacja CLI](cli-reference.md).

### `selects`

Ustawienia nakładki DOM na natywne selecty, używanej przez krok
[`select`](#select). Wszystkie pola są opcjonalne; pominięcie całego bloku
`selects:` zachowuje wbudowane zachowanie nakładki.

```yaml
selects:
  mode: shim            # shim (domyślnie) | native
  settleMs: 1000
  maxVisibleOptions: 8
  openHoldMs: 350
```

| Pole | Domyślnie | Znaczenie |
|---|---:|---|
| `mode` | `shim` | Globalna furtka. `shim` zamienia surowe `<select>` na nakładkę DOM, więc ich listy są widoczne na filmie; `native` wraca wszędzie do sprzed-nakładkowego przełączania wartości strzałkami. Furtka na poziomie kroku `select.mode` nadpisuje to dla jednej kontrolki. |
| `settleMs` | `1000` | Milisekundy odczekiwane po załadowaniu strony, zanim każdy `<select>` zostanie sklasyfikowany jako surowy albo już ulepszony przez stronę. Daje własnej inicjalizacji strony (select2/Tom Select/Chosen) czas na ukrycie lub podmianę oryginalnej kontrolki, zanim nakładka zdecyduje, czy jej dotknąć. |
| `maxVisibleOptions` | `8` | Liczba opcji widocznych naraz w rozwiniętej liście, zanim zacznie się przewijać wewnętrznie. |
| `openHoldMs` | `350` | Milisekundy, przez które rozwinięta lista pozostaje otwarta, żeby widz zdążył ją przeczytać, zanim kursor ruszy do wybranej opcji. |

Do skompilowanego wyniku wpływa tylko `mode`: zmiana na `native` zmienia to, co
steruje resolver, więc — podobnie jak `config.setup` — wchodzi do config hasha
tylko wtedy, gdy różni się od domyślnej wartości, i wymusza rekompilację (patrz
[macierz przebudowy](#macierz-przebudowy)). `settleMs`, `maxVisibleOptions` i
`openHoldMs` to kosmetyczne dostrojenie renderu, jak `cursor` czy `popup` — ich
zmiana nigdy nie wymaga rekompilacji.

## Reguła kroku

Krok ma najwyżej jedną komendę główną spośród `teach`, `navigate`, `click`, `hover`,
`enterText`, `select`, `scroll`, `wait`, `slide` i `closeWindow`. `say` może być jedyną treścią kroku albo
towarzyszyć jednej akcji. Pusty krok i dwie akcje główne są błędem.

Krok może dodatkowo nieść znacznik `optional: true`, a element listy `steps` może być
blokiem `when` zamiast kroku — patrz [Gałęzie opcjonalne](#galezie-opcjonalne).

Narracją domyślną jest `say`, a gdy go nie ma — `teach`. Same `click`, `hover`,
`enterText`, `navigate`, `wait`, `slide` i `closeWindow` nie są czytane — tekst planszy
`slide` jest wyświetlany, nie wypowiadany.

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

### `select`

```yaml
- select:
    from: "lista rozwijana Rodzaj raportu"
    option: "tabela"
  say: "Z listy rodzaj raportu wybieram format tabelaryczny."
```

Wybór opcji z listy rozwijanej. `from` to semantyczny opis celu wysyłany do
reasonera i musi wskazać natywny element `<select>` — trafienie w inną kontrolkę
(własny widżet `role="combobox"`, przycisk) to błąd walidacji `not_select`. `option`
to widoczna etykieta wybieranej opcji; jest pokazywana, nigdy czytana i **nie**
podlega podstawianiu zmiennych środowiskowych.

Listę opcji natywnego `<select>` rysuje system operacyjny, więc żadne narzędzie do
automatyzacji przeglądarki — w tym Playwright — nie rozwinie jej ani nie zrzuci na
ekran. Żeby mimo to pokazać wybór na filmie, Guidebot wstrzykuje nakładkę DOM
(`config.selects`, opisana niżej), która zamienia surową kontrolkę na widżet z listą
naprawdę rozwijaną w dół, w DOM-ie. Podczas `render` kursor dojeżdża do kontrolki i
klika ją, lista się rozwija, kursor dojeżdża do wybranego wiersza i klika go — dwa
widoczne gesty zamiast niewidocznej zmiany wartości. Podczas `compile` wartość
ustawiana jest wprost, bez animacji — kompilacja ma być szybka, nie efektowna. Tak
czy inaczej element kończy na `option`, więc kolejne kroki i render są zgodne.

**Widżety, które strona sama już ulepszyła, są celowo pozostawiane bez zmian.**
Jeśli aplikacja docelowa podmienia swój `<select>` na własny dropdown — select2,
Tom Select, Chosen albo cokolwiek o tym samym kształcie (ukryty lub niewidoczny
oryginalny `<select>` plus sąsiadujący widżet) — Guidebot go nie nakłada: lista
takiego widżetu jest już w DOM-ie i już nagrywa się poprawnie. Ta sama choreografia
steruje wtedy bezpośrednio widoczną kontrolką powiązaną z ukrytym selectem
(znalezioną po jej `aria-controls`/`aria-owns`, powiązaniu zwrotnym
`aria-labelledby`/`aria-describedby`, a w ostateczności po najbliższym widocznym
rodzeństwie), a potem wierszem opcji, który pojawia się po jej otwarciu.

Jeśli żaden z tych dwóch kroków nie znajdzie niczego do kliknięcia — brak widocznej
kontrolki powiązanej z już ulepszonym selectem albo brak wiersza pasującego do
`option` po otwarciu listy — przebieg kończy się **błędem**, zamiast po cichu
ustawić wartość. `compile` sprawdza też z góry, czy ulepszony widżet da się
sterować, więc niesterowalny widżet wychodzi na jaw tam, a nie w połowie renderu.

Dla widżetu, którego nakładka naprawdę nie potrafi obsłużyć — np. listy
doszukującej opcje przez sieć w miarę pisania — użyj furtki na poziomie kroku,
**`mode: native`**. Przywraca ona zachowanie sprzed nakładki: kursor nadal dojeżdża
do kontrolki i klika ją, ale zwinięta wartość jest *przełączana* do `option`
strzałkami zamiast otwierać listę (skok o więcej niż dwanaście pozycji ustawiany
jest od razu, by animacja nie trwała zbyt długo). `mode` ma też formę globalną,
`config.selects.mode` (patrz sekcja `selects` niżej); wartość na poziomie kroku
domyślnie ją dziedziczy i nadpisuje ją dla jednej upartej kontrolki w reszcie
poprawnego scenariusza:

```yaml
- select:
    from: "lista województw"
    option: "Mazowieckie"
    mode: native          # opcjonalne; domyślnie config.selects.mode
```

### `scroll`

```yaml
- scroll: down                    # up | down | top | bottom
- scroll: { to: down, amount: 300 }   # forma obiektowa; amount w pikselach
  say: "Przewijam w dół, aby pokazać podgląd wyników."
```

Przewinięcie strony — wizualny krok tylko na etapie `render`, bez celu dla agenta,
podobnie jak liczbowy `wait`. `to` przyjmuje `up`, `down`, `top` lub `bottom`; skrót
`scroll: down` działa dla wszystkich czterech. `amount` (piksele) reguluje przewinięcie
`up`/`down` i jest ignorowane dla `top`/`bottom`; bez niego `down`/`up` przewija o
większość wysokości okna.

Służy do wprowadzenia w kadr treści spod „linii zgięcia" — zwłaszcza treści, których
resolver **nie** potrafi wskazać, jak podgląd w `<iframe>` czy lista opcji natywnego
selecta. Kursor nadal nie wejdzie do iframe, ale przewinięcie wprowadza go w kadr. Z
nakładką (render) przewijanie jest animowane; podczas `compile` skacze wprost. Ponieważ
nie rozwiązuje żadnego elementu, `scroll` nie wymaga `compile` i nie przyjmuje `optional`.

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

Element, który pojawia się z opóźnieniem, wymagał wcześniej poprzedzającego `wait`
liczbowego. Jeżeli element jest naprawdę warunkowy (raz jest, raz go nie ma), użyj
zamiast tego [gałęzi opcjonalnej](#galezie-opcjonalne): bramka `when` sama odpytuje
stronę i znosi nieobecność elementu.

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
**wymaga `guidebot compile`**; render sprawdza liczbę kroków przed startem i kończy się
błędem przy nieaktualnym sidecarze. To samo dotyczy `closeWindow` (patrz niżej).

### `closeWindow`

```yaml
- teach: "Klikamy odnośnik, który otwiera się w nowej karcie"
- say: "Przeczytaliśmy zawartość, wracamy."
- closeWindow: true
```

Zamyka **aktywne** okno i wraca do tego, które je otworzyło. Przyjmuje wyłącznie
wartość `true`; `closeWindow: false` jest błędem walidacji, nie cichym brakiem
działania. Bez otwartego okna krok kończy się błędem.

Nowe okno powstaje samo, gdy kliknięcie na stronie je otworzy — przez `window.open`
albo link `target="_blank"`. Guidebot rozpoznaje je po `opener()`, więc link
z `rel="noopener"` (który zeruje `opener()`) **nie** zostanie rozpoznany jako
otwierający okno. Okno wypełniające cały kadr (np. karta `target="_blank"`, która nie
poprosiła o rozmiar) jest pokazywane pełnoekranowo z własnym paskiem adresu; mniejsze
okno `window.open` zachowuje pływającą prezentację. Sam scenariusz nie otwiera okna —
nie ma komendy „otwórz okno".

Jak `slide`, `closeWindow` zmienia liczbę kroków, więc **wymaga `guidebot compile`**.
Pełny przykład: [`examples/newwindow/`](https://github.com/iplweb/guidebot-recorder/tree/main/examples/newwindow).

### `desktop`

```yaml
- desktop:
    icon: chrome                     # opcjonalne; wbudowana nazwa lub ścieżka do pliku
    label: Przeglądarka internetowa  # opcjonalne; podpis pod ikoną
    hold: 1.0                        # opcjonalne; sekundy zatrzymania na otwartym oknie
  say: "Otwieramy przeglądarkę."     # opcjonalna narracja
```

Symulowany „pulpit" otwierający film: kursor podjeżdża po łuku do ikony
przeglądarki, klika dwa razy, a z ikony wyrasta okno, które odsłania pasek
przeglądarki. Wizualny jak `slide` — kompiluje się do niczego, więc
**wymaga `guidebot compile`** wyłącznie dlatego, że dodaje/przesuwa krok (render
sprawdza liczbę kroków). Zwykle jest pierwszym krokiem; kolejny `navigate` wpisuje
adres w odsłonięty pasek.

Kolor tła pulpitu jest ustawieniem filmu, nie krokiem — `config.desktop.color`
(domyślnie granatowy `#1f3a63`), więc każdy krok `desktop` w filmie ma to samo tło.

`icon` przyjmuje **wbudowaną nazwę** albo **ścieżkę do własnego pliku**
(`.svg/.png/.jpg/.gif/.webp`; ścieżki względne liczone od katalogu scenariusza).
Wbudowane ikony to celowo **rodzajowe, własnoręcznie narysowane** grafiki — nie
prawdziwe logotypy przeglądarek (to znaki towarowe, a pakiet jest
redystrybuowalny); nazwa mówi tylko, którą przeglądarkę ikona przywołuje:

| Nazwa | Rysunek |
|---|---|
| `chrome`, `browser` | kolorowy pierścień z niebieskim środkiem |
| `firefox`, `flame` | płomień |
| `iexplore`, `edge`, `legacy` | niebieskie „e" |
| `globe` | prosty globus |

Aby użyć prawdziwego logo, wskaż `icon` na własny plik — wtedy nic nie jest
dystrybuowane z pakietem.

### `expect`

Model przyjmuje pole `expect`, lecz compiler sam wyprowadza gotowość z obserwowanej
zmiany URL i nie traktuje źródłowej wartości jako stabilnego sterowania. Nie używaj
`expect` w scenariuszach; dla SPA dodaj jawny `wait`.

## Gałęzie opcjonalne {#galezie-opcjonalne}

Część przepływu bywa naprawdę warunkowa. Sztandarowy przypadek to banner zgody na
cookies: raz się pokazuje, raz nie — zależnie od zapisanej zgody, testu A/B albo
geolokalizacji. Bez jawnego oznaczenia taki krok jest twardym błędem: `wait` przekracza
timeout i cały przebieg pada.

Gałąź opcjonalna oznacza grupę kroków jako „wykonaj tylko wtedy, gdy ten element jest
na stronie". Gdy elementu nie ma, gałąź jest pomijana — razem z narracją, która znika z
osi czasu, a nie zostaje jako cisza — a kolejne kroki wykonują się normalnie.

### Blok `when`

Blok `when` stoi w `steps` na tym samym poziomie co zwykłe kroki:

```yaml
steps:
  - navigate: https://www.example.com

  - when: "banner zgody na cookies"
    state: visible
    timeout: 20
    steps:
      - teach: "Kliknij przycisk przechodzący dalej do serwisu"
      - say: "Akceptujemy cookies i ruszamy dalej."

  - teach: "Kliknij ikonę konta"        # wykonuje się zawsze
```

| Pole | Wymagane | Domyślnie | Znaczenie |
|---|---:|---:|---|
| `when` | Tak | — | Semantyczny opis elementu-bramki. |
| `state` | Nie | `visible` | `visible`, `hidden`, `enabled` — jak w warunkowym `wait`. |
| `timeout` | Nie | `10.0` | Sekundy oczekiwania na element, zanim uznamy go za nieobecny. |
| `steps` | Tak | — | Kroki wykonywane po spełnieniu bramki. |

Bramka zachowuje się jak warunkowy `wait`, którego timeout nie jest błędem. Daj
bannerowi wystarczający `timeout` — z zewnątrz bramka wolna i bramka nieobecna wyglądają
tak samo.

Bloków `when` **nie wolno zagnieżdżać**. `when` wewnątrz `steps` innego bloku jest błędem
walidacji. Nie ma `else` ani gałęzi alternatywnej.

### `optional: true` na pojedynczym kroku

Dla jednego warunkowego kroku zamiast bloku wystarczy `optional: true`:

```yaml
- click: "link „zamknij" na pasku powiadomienia"
  optional: true
```

Pole jest dozwolone na krokach rozwiązujących target — `teach`, `click`, `hover`,
`enterText` i warunkowy `wait` — oraz na liczbowym `wait`. Na kroku z samym `say`, na
`navigate` i na `slide` jest **błędem walidacji**: te kroki niczego nie rozwiązują, więc
„opcjonalność" obiecywałaby tolerancję, której Guidebot nie daje.

### Compile i render

`guidebot compile` nie kończy się błędem, gdy elementu-bramki nie ma. Zapisuje bramkę i
wszystkie kroki gałęzi jako *pending* w sidecarze, wypisuje ostrzeżenie i kończy się
kodem `0`. Wpis pending liczy się jako aktualny, więc kolejny `compile` nie uruchamia
przeglądarki tylko po to, żeby znów przepalić timeout bramki — do ponownej próby użyj
`--force`.

`guidebot render` obsługuje gałąź pending **w miejscu**: jeśli bramka jednak się pojawi,
render woła reasoner, odpytuje stronę aż element się rozwiąże albo minie `timeout`,
wykonuje kroki gałęzi i przepisuje `.compiled.yaml` — każdy następny render tej gałęzi
jest już deterministyczny i bez AI. Gdy reasoner jest niedostępny, render głośno ostrzega
i pomija gałąź, zamiast padać.

### Granica błędów

Opcjonalność nie znaczy „ignoruj błędy". Za *nieobecność elementu* liczą się wyłącznie te
sygnały:

| Sytuacja | Liczy się jako nieobecność |
|---|---|
| Bramka ze skompilowaną akcją | `TimeoutError` Playwrighta z oczekiwania |
| Bramka nadal pending | Minie okno odpytywania albo reasoner odpowie `no_action` / `no_handle` |
| Krok opcjonalny, nadal pending | Reasoner odpowie `no_action` / `no_handle` |
| Krok opcjonalny ze skompilowaną akcją | Zapisany target nie przechodzi walidacji ponownego użycia |

Wszystko poza tym nadal wywala render. W szczególności **`multiple_actions` — czyli
niejednoznaczny opis targetu — jest twardym błędem**, tak samo w gałęzi opcjonalnej jak
poza nią. Niejednoznaczny opis to błąd autora, a nie brakujący element; przemilczenie go
pozwoliłoby literówce po cichu usunąć całą gałąź z filmu.

Błędy *wewnątrz* rozpoczętej gałęzi też są śmiertelne: nieudane kliknięcie w rozwiązany
już target, błąd nawigacji czy timeout `wait` na kroku, który nie jest bramką, kończą
render jak zwykle.

!!! warning "Znane ograniczenia"

    **Render, który sam naprawia gałąź, zamraża klatkę.** Render nagrywa czas
    rzeczywisty, więc wywołanie reasonera w miejscu — nawet do dwóch minut — zamraża
    klatkę w filmie, a nieobecna gałąź kosztuje tyle sekund pustki, ile wynosi jej
    `timeout`. Render, który po raz pierwszy rozwiązuje gałąź, traktuj jako
    jednorazowy — czysty film uzyskasz z kolejnego renderu. Wycięcie tych przestojów z
    osi czasu jest planowane.

    **Pop-upy wewnątrz gałęzi opcjonalnej nie są wspierane.** Kliknięcie rozwiązane
    dopiero podczas renderu nie niesie obserwacji `opens_popup` z compile, więc pop-up
    otwarty z wnętrza gałęzi kończy render błędem „unexpected popup". Kliknięcia
    otwierające pop-up trzymaj poza gałęziami opcjonalnymi.

## Macierz przebudowy

| Zmiana | Wymaga `guidebot compile`? |
|---|---:|
| `cursor` (rozmiar, `click`, wyśrodkowany start) | Nie — render-only |
| `typing`, `sound`, `intro`, `chrome` | Nie — render-only |
| `holdFrameForNarration`, `holdFrameSettle` | Nie — render-only |
| `verifyUserLoggedIn`, `maxAgeHours` (na setupie) | Nie — render-only, poza config hashem |
| Istniejący tekst narracji `say`/`teach`, `translations` | Nie — render-only |
| Sama wartość `enterText.text` | Nie — render-only |
| Sama wartość `select.option` | Nie — render-only |
| `config.selects.settleMs`, `maxVisibleOptions`, `openHoldMs` | Nie — render-only |
| `config.setup` (na celu) dodane, usunięte lub przepięte | Tak — ścieżka `setup` wchodzi do config hasha celu |
| `config.selects.mode` przełączone między `shim` i `native` | Tak — wchodzi do config hasha, jak `config.setup`, tylko gdy różni się od domyślnej wartości |
| Dodanie, usunięcie lub zmiana kolejności kroku `slide` | Tak |
| Instrukcja targetu kroku (zdanie `teach`, `click`/`hover`, `enterText.into`, `select.from`, `wait.until`/`state`) lub własne `select.mode` kroku | Tak |
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
