# Rozwiązywanie problemów

Diagnostykę zacznij od widocznej, pełnej kompilacji:

```bash
uv run guidebot compile scenarios/flow.scenario.yaml \
  --force --headed --pause-on-error --verbose
```

## Jak czytać komunikat o kroku

Każdy komunikat — ostrzeżenie, błąd i błąd walidacji — pokazuje plik, numer linii
i dosłowny fragment YAML, więc kroków nie trzeba odliczać ręcznie:

```
⚠ krok 3/8 — examples/onet-login.scenario.yaml:37 (bramka `when:`)
     37 |   - when: "the cookie consent banner"
     38 |     state: visible
     39 |     timeout: 20
   element bramkujący nie pojawił się — zapisano wpis oczekujący (pending)
```

Numer w nagłówku to pozycja w **kolejności wykonania**, a nie pozycja na liście
`steps:`. Te dwie liczby rozjeżdżają się, bo każdy blok `when:` wnosi dodatkowy
krok bramkujący (widoczny w nagłówku jako `(bramka `when:`)`), którego w pliku
nie ma jako osobnego wpisu. Kroki wewnątrz bloku są oznaczone
`(w bramce z linii N)`. Wiążąca jest linia, nie numer.

Błędy walidacji dokładają karetkę pod linią, której dotyczy problem:

```
BŁĄD walidacji — scenarios/flow.scenario.yaml:23 (krok 5/12)
     23 |   - click: "Zapisz"
          ^ tutaj
     24 |     navigate: "https://example.test"
   krok ma 2 komend (['navigate', 'click']); dozwolona dokładnie jedna
```

Fragment pochodzi z pliku **sprzed** podstawienia `${ZMIENNA}`, więc widać
w nim nazwę zmiennej, nigdy jej wartość.

## Codex nie działa

```bash
npm install -g @openai/codex
codex --version
codex login
codex login status
```

Guidebot uruchamia lokalne `codex`; sama aplikacja lub rozszerzenie IDE nie gwarantuje
programu w `PATH`. Używa zapisanej sesji Codex i nie ma osobnej konfiguracji logowania.

## Compile mówi, że wszystko jest aktualne po zmianie strony

Szybka ścieżka poprawnie sprawdza źródło, compiler v2, liczbę slotów, rodzaj targetowej
komendy, instrukcję i config hash, ale nie otwiera strony. Nie zobaczy zmiany DOM,
danych, cookies ani wpływu zmienionego kroku `navigate`. Użyj `--force`.

## Sidecar ma wersję 1 albo render mówi „compiled jest nieaktualny”

Plik bez wersji jest traktowany jako v1. Render v2 przed TTS sprawdza nazwę źródła,
wersję, wyrównanie akcji i fingerprinty. Nie poprawiaj pliku ręcznie:

```bash
uv run guidebot validate scenarios/flow.scenario.yaml
uv run guidebot compile scenarios/flow.scenario.yaml --force --headed -v
```

Przejrzyj i commituj nowy sidecar. Podczas renderu zwykłe akcje dodatkowo sprawdzają
tożsamość na żywo; warunkowy `waitFor` jest wyjątkiem.

## Reasoner nie znajduje elementu

1. Dopasuj viewport i `locale` do oczekiwanego układu.
2. Opisz rolę, widoczną nazwę, etykietę, sekcję lub funkcję elementu.
3. Umieść jedną akcję i jeden target w kroku.
4. Dodaj liczbowy `wait` przed treścią pojawiającą się później.
5. Sprawdź stan przez `--headed --pause-on-error -v`.
6. Upewnij się, że element nie jest w iframe.

Snapshot jest ograniczony do 200 semantycznych kandydatów i zwykle do elementów
widocznych w viewportcie. Guidebot może przewinąć do już rozwiązanego targetu, ale nie
ma źródłowej komendy `scroll`.

## Namiar pasuje do kilku elementów

Gdy kilka kontrolek ma tę samą rolę i nazwę dostępną, reasoner wskazuje kandydata,
którego ma na myśli, a kompilator mierzy jego pozycję i zamraża pozycyjny `nth`.
Kompilacja i tak kończy się sukcesem, ale wypisuje ostrzeżenie z liczbą trafień:

```
namiar pozycyjny (2 z 11 pasujących, nth=1) — rozważ doprecyzowanie opisu, żeby wskazywał element jednoznacznie
```

To nie jest błąd — krok się wyrenderuje. Ostrzeżenie sygnalizuje namiar tak stabilny,
jak pozycja elementu: późniejsza przebudowa strony, która dołoży albo przestawi pasujące
rodzeństwo, przesunie indeks.

Część takich przesunięć Guidebot wykrywa przy kolejnym `compile`: zamrożony indeks jest
sprawdzany względem elementu, do którego był przypięty, więc gdy cel zniknie albo trafi
w inne miejsce struktury strony, wpis zostaje unieważniony i namiar rozwiązany od nowa.
**Nie wykrywa jednak przypadku najbardziej podstępnego** — dołożenia kolejnego,
strukturalnie identycznego wiersza przed celem. Wtedy element, który wchodzi na
zamrożoną pozycję, wygląda z punktu widzenia struktury tak samo jak poprzedni, więc
żaden sygnał nie powstaje, a krok po cichu zaczyna dotyczyć innego wiersza.

Dlatego ostrzeżenie warto potraktować poważnie, a nie odłożyć na później. Żeby je
usunąć, uczyń cel jednoznacznym: doprecyzuj opis (dopisz nagłówek sekcji, etykietę
wiersza albo funkcję kontrolki) lub nadaj kontrolce nazwę dostępną. To jedyne, co
usuwa kruchość, zamiast ją zgłaszać.

Jeśli reasoner w ogóle nie potrafi wskazać jednego elementu — opis pozostaje
niejednoznaczny i nic nie odróżnia bliźniaczych kontrolek — krok kończy się po
wyczerpaniu prób błędem:

```
nie udało się zwalidować namiaru dla: '...'
```

To celowa twarda porażka: niejednoznaczny namiar, który dawniej bywał błędnie zamrażany
— i wychodził dopiero, gdy człowiek obejrzał gotowy film — teraz zatrzymuje kompilację.
Napraw go tak samo, precyzyjniejszym opisem albo nazwą dostępną kontrolki, tak żeby
opisowi odpowiadał dokładnie jeden element.

Ten sam błąd ma drugą, mniej oczywistą przyczynę: **element poza kadrem**. Migawka
kandydatów obejmuje wyłącznie to, co widać w viewporcie, i najwyżej 200 elementów, więc
kontrolka przewinięta poniżej krawędzi okna nie ma identyfikatora, którym reasoner mógłby
ją wskazać — a bez niego niejednoznacznego namiaru nie da się już przypiąć. Dawniej
zgadywany indeks bywał w takiej sytuacji przypadkiem trafiony; teraz krok kończy się
błędem, zamiast po cichu wskazać inny element. Poprzedź go przewinięciem (`scroll`) albo
`wait`, żeby cel znalazł się w kadrze — tak samo jak przy pozostałych objawach z tej
sekcji.

## `select` trafia w złą listę rozwijaną

Objaw to `option_missing` w komunikacie kompilacji, który — jak każdy komunikat
kroku — wskazuje linię do poprawienia:

```
krok 6/12 — scenarios/flow.scenario.yaml:23
     23 |   - select:
     24 |       from: "lista wyboru charakteru formalnego"
     25 |       option: "Artykuł w czasopismie"
   nie udało się zwalidować namiaru dla: 'lista wyboru charakteru formalnego'
   (ostatnie odrzucenie: The <select> has no option labelled 'Artykuł w czasopismie';
    it offers: 'Raport jednostki', 'Raport autora'.)
```

Resolver wskazał `<select>`, który nie ma żądanej opcji. Najczęściej dzieje się to
tam, gdzie listy nie mają nazwy dostępnej i mogą być namierzone tylko pozycyjnie
(`combobox nth=N`) — dodany wiersz, dodana ramka albo AJAX podmieniający widget
przesuwa numerację i ten sam opis wskazuje inny element.

1. Sprawdź, czy `option` dokładnie odpowiada etykiecie w interfejsie. Białe znaki nie
   mają znaczenia (ich ciągi są normalizowane po obu stronach), ale **wielkość liter
   ma** — tak samo jak cała reszta. To ta sama reguła, którą stosuje każda ścieżka
   wykonania, więc etykieta przyjęta tutaj jest etykietą, którą Guidebot naprawdę
   potrafi wybrać, a odrzucona i tak przewróciłaby się przy odtwarzaniu.
2. Doprecyzuj `from`: dopisz nagłówek sekcji, etykietę wiersza lub funkcję listy, żeby
   opis odróżniał ją od pozostałych list na stronie.
3. Dodaj `wait` przed krokiem, jeśli listę dostawia AJAX — resolver wybiera spośród
   tego, co widzi w chwili kompilacji.

Lista etykiet w komunikacie pokazuje, na jaki element faktycznie trafił resolver, więc
zwykle od razu widać, o który `<select>` chodzi.

Kontrola celowo nie działa dla `<select>`, który strona przejęła własnym widżetem
(select2 i podobne): takie kontrolki Guidebot obsługuje przez listę DOM strony, a nie
przez opcje ukrytego oryginału, więc zestaw opcji pusty do czasu otwarcia widżetu nie
jest dowodem trafienia w zły element. Pomyłka wychodzi wtedy dopiero przy `render`, w
postaci wiersza opcji, który się nie pojawia — patrz
[`select` w referencji scenariusza](scenario-reference.md#select).

## `teach` → `type` jest odrzucane

Jawny `inputText` musi być niepustym, dokładnym fragmentem instrukcji `teach`. Guidebot
odrzuca placeholder ENV, słowa sugerujące sekret oraz targety wyglądające na hasło,
token, PIN, kod jednorazowy lub dane karty. Dla takich wartości użyj `enterText.text`
z ENV. Po zmianie literału ponownie skompiluj target.

## Popup nie działa

Popup musi otworzyć się wskutek konkretnej akcji `click`. Nie dodawaj kroku
„przełącz okno” — przełączenie i powrót są automatyczne.

Błąd jest zamierzony, gdy:

- okno otwiera się przed kliknięciem, za późno albo poza akcją;
- jedno kliknięcie otwiera kilka okien;
- w scenariuszu pojawia się drugi popup, nawet po zamknięciu pierwszego;
- popup zamyka się asynchronicznie, a nie wskutek kroku;
- strona główna zostaje zamknięta;
- render oczekiwał `opens_popup`, lecz okno się nie pojawiło.

Po zmianie zachowania popupu uruchom `compile --force`. Obsługiwany film może mieć
sekwencję `main → popup → main`; natywne karty Chromium nie są nagrywane.

## Język strony nadal jest niewłaściwy

Compile i render przekazują to samo `config.locale` do świeżego kontekstu. Jeżeli
aplikacja wybiera język przez host, ścieżkę, konto, cookie albo redirect, ustaw również
ten stan. Dla różniących się UI użyj pełnych scenariuszy w
[render-set](localized-render-sets.md), a nie tylko `translations`.

## Brakuje tłumaczenia albo ścieżki audio

Każdy narracyjny krok musi mieć dokładnie klucze wszystkich `audioTracks[].lang`.
Nie dodawaj `translations` do kroku bez `say`/`teach`. Każda ścieżka musi mieć unikalne
`lang` i `trackLanguage`; przy wielu ścieżkach `trackLanguage` jest wymagane również
dla domyślnego `tts`. Standardowe CLI wymaga providera `edge` na wszystkich
ścieżkach. Zobacz [Wiele ścieżek audio](multilingual-audio.md).

## `render-set` zatrzymuje się przed Chromium

To preflight bezpieczeństwa. Sprawdź, czy:

- klucz wariantu równa się `config.locale` i `config.tts.lang`;
- wariant ma `tts.trackLanguage`, nie ma `audioTracks` i używa tego samego providera;
- ścieżki są względne, bez `..`, dysku Windows, backslasha i ucieczki przez symlink;
- scenariusze, sidecary, outputy i `.guidebot_video/<stem>` nie kolidują;
- wszystkie sidecary są aktualne po udanym `compile-set`;
- stockowy `render-set` ma provider `edge`.

Nie ma `validate-set`; pełny manifest jest ładowany przez oba polecenia zestawu. Błąd
drugiego wariantu nie usuwa gotowego pierwszego i nie uruchamia trzeciego.

## `${ZMIENNA}` nie jest rozwijane

Substytucja działa tylko w tekstowym `navigate`, `navigate.url` i `enterText.text`.
Nie działa w manifeście, `baseUrl`, narracji ani opisie targetu. Wyeksportuj zmienną w
procesie; `.env` nie jest ładowany. `$${` oznacza literalne `${`.

## TTS, ffmpeg albo ffprobe zgłasza błąd

```bash
ffmpeg -version
ffprobe -version
```

Edge TTS wymaga sieci przy braku segmentu w `.guidebot/audio/`. Adapter używa do
syntezy `voice`; `model` i `speed` są obecnie ignorowane, choć należą do klucza cache.
Cache przechowuje tekst narracji w JSON. Standardowe CLI odrzuca provider inny niż
`edge` zamiast go ignorować.

Po udanym renderze WAV-y pozostają w
`<output-dir>/.guidebot_video/<stem>/bed-<trackLanguage>.wav`. Publikacja MP4 i pełnego
zestawu WAV jest atomowa; błąd nie powinien zastąpić poprzedniego mastera.

## Warunkowy `wait` zachowuje się inaczej niż oczekiwano

- Target zwykle musi istnieć i być widoczny już podczas compile; poprzedź go liczbową
  pauzą, jeśli pojawia się później.
- Jeśli element może się w ogóle nie pojawić (banner cookies, interstitial), użyj
  gałęzi opcjonalnej `when` zamiast liczbowej pauzy — odpytuje o element i pomija
  swoje kroki, gdy ten nie wystąpi, zamiast wywracać przebieg. Zobacz
  [Gałęzie opcjonalne](scenario-reference.md#galezie-opcjonalne).
- `hidden` może poprawnie nie mieć zamrożonej tożsamości.
- `enabled` obecnie czeka na widoczność, a nie osobno na stan aktywności.
- Dla przejścia SPA bez zmiany URL dodaj jawny wait po akcji.

## Syntetyczny pasek zmienia układ albo URL

`config.chrome` wstrzykuje nakładkę DOM wyłącznie podczas renderu — to nie jest
prawdziwy interfejs Chromium. Kropki okna są dekoracyjne, a cała nakładka ma
`pointer-events: none`.

Pasek zajmuje `chrome.height` pikseli, zwiększając górny padding `<html>` w
obrębie tego samego viewportu — wymiary MP4 się nie zmieniają. Elementy `sticky`/
`fixed` albo breakpoint RWD mogą więc wyglądać inaczej niż podczas `compile`, które
nigdy nie wstrzykuje paska, nawet z `--headed`. Zmień viewport albo wyłącz pasek,
jeśli to psuje przebieg.

Adres synchronizuje się przy nawigacji i przy najbliższym `ensure` nakładki, nie
przy każdej zmianie History API czy hasha — przez chwilę narracji może być więc
widoczny `about:blank`, zanim padnie pierwszy `navigate`. Pełny URL, łącznie z
query i fragmentem, trafia do filmu — dla adresów z sekretem ustaw `showUrl: false`.
Kłódka jest czysto dekoracyjna i pojawia się dla każdego `https:`, niezależnie od
faktycznego bezpieczeństwa strony.

## Obecne ograniczenia

- Codex CLI jest jedynym wbudowanym reasonerem; brak `--reasoner` i `--model`.
- Chromium jest jedyną przeglądarką standardowego CLI.
- Jedna sesja obsługuje najwyżej jeden popup otwarty przez kliknięcie; brak jawnego
  przełączania kart i obsługi iframe.
- Brak route discovery, ręcznego recordera i auto-heal.
- Edge TTS jest jedynym adapterem standardowego CLI.
- Render-set jest sekwencyjny, bez filtrowania wariantu i transakcji całego zestawu.
- Candidate snapshot jest ograniczony do 200 elementów i zorientowany na viewport.
- ENV nie maskuje wartości w filmie ani logach aplikacji.
- `.guidebot/audio/` i `.guidebot_video/` pozostają do ręcznego usunięcia.

## Dokumentacja nie buduje się

```bash
uv sync --group docs
uv run --group docs mkdocs build --strict
```

Polski i angielski mają osobne pliki bez fallbacku. Brak tłumaczenia, linku lub wpisu
nawigacji należy poprawić, a nie ukrywać.
