# PDF krok-po-kroku (`guidebot guide`) — projekt

Data: 2026-07-20
Gałąź: `feat/pdf-step-guide`
Status: zatwierdzony projekt (spec)

## Cel

Wygenerować z tego samego scenariusza, z którego powstaje wideo, statyczny
przewodnik PDF „krok po kroku”: dla każdego istotnego kroku jedna strona w
orientacji poziomej (landscape) ze zrzutem ekranu po lewej (z adnotacjami
pokazującymi, gdzie jest kursor, w co się klika i co się wpisuje) oraz opisem
po prawej.

PDF jest osobnym produktem — nie zależy od renderu wideo i nie zmienia go.

## Zakres (v1) i świadome pominięcia

W zakresie:

- Nowa komenda CLI `guidebot guide`.
- Własny, lekki przebieg przechwytywania oparty o istniejący `Recorder`.
- Zrzuty per krok + adnotacje (strzałka ruchu, kółko kliknięcia, ramka
  wpisanego tekstu, poświata hover).
- Jeden krok = jedna strona landscape (zrzut po lewej, tekst po prawej).
- Składanie PDF przez Chromium `page.pdf()` z kompozycji HTML.
- Jeden język (kanoniczny `say`/`teach`), z opcjonalnym polem `caption`.

Poza zakresem (świadomie, do ewentualnego v2):

- Grupowanie wielu kroków na jednej stronie z numerowanymi dymkami (1)(2).
- Wielojęzyczność / zestaw PDF-ów per język (zrzuty można współdzielić —
  łatwe do dołożenia później).
- Wypalanie adnotacji przez Pillow + układ przez reportlab (odrzucone:
  gorszy efekt, dwie dodatkowe zależności).

## Architektura

Zero nowych ciężkich zależności: Playwright/Chromium już jest w projekcie,
więc i przechwytywanie, i renderowanie PDF idzie przez Chromium.

Nowy pakiet `guidebot_recorder/guide/`:

- `capture.py` — przebieg przechwytywania: otwiera Chromium, odtwarza
  skompilowany scenariusz, dla każdego kroku ustawia kursor przez `Recorder`,
  robi `page.screenshot()` i produkuje `GuidePage` z geometrią adnotacji.
- `annotate.py` — czysta geometria adnotacji: z bounding-boxów i pozycji
  kursora liczy współrzędne strzałki / kółka / ramki (bez I/O, testowalne
  jednostkowo).
- `layout.py` — składa listę `GuidePage` w jeden dokument HTML (CSS grid:
  panel zrzutu z warstwą SVG po lewej, tekst po prawej; jedna strona
  landscape na krok).
- `pdf.py` — renderuje HTML do PDF przez Chromium `page.pdf({landscape: true})`.
- `guide.py` — publiczne wejście `run_guide(...)`, spina powyższe; wywoływane
  z CLI.

Model danych (wewnętrzny, nie serializowany na dysk):

```python
class Annotation(NamedTuple):
    kind: Literal["arrow", "click", "typed", "hover"]
    # współrzędne w pikselach zrzutu (układ CSS strony)
    ...  # np. arrow: (x1,y1,x2,y2); click/hover: (cx,cy,r); typed: (x,y,w,h)

class GuidePage(NamedTuple):
    kind: Literal["step", "navigate", "slide"]
    screenshot: Path | None      # None dla strony-przekładki slide
    text: str                    # opis po prawej
    heading: str | None          # np. "Otwórz adres X", tytuł slajdu
    annotations: list[Annotation]
    screenshot_size: tuple[int, int]  # do skalowania warstwy SVG
```

## Przepływ danych

1. `run_guide(path, out_pdf, browser, ...)` ładuje `Scenario` i
   `CompiledScenario` (jak `run_render`: `flat = scenario.flat_steps()`
   zipowane 1:1 z `compiled.actions`).
2. `capture.py` iteruje `flat`. Utrzymuje `prev_cursor: (x,y) | None`.
   Per krok, wg `step.command_kind()`:
   - `click`/`hover`/`enter_text`/`teach`: `Recorder._point_and_prepare`
     ustawia kursor na środku celu (`bounding_box()`); zapamiętujemy
     `target_center` i `target_box`. Dla `enter_text`/`teach` wpisujemy tekst,
     a potem robimy zrzut (ramka wokół pola). Adnotacje:
     - `arrow` od `prev_cursor` do `target_center` (jeśli `prev_cursor`),
     - `click` (kółko) dla click, `hover` (poświata) dla hover,
     - `typed` (ramka) dla wpisywania.
     `prev_cursor := target_center`.
   - `navigate`: wykonaj nawigację (jak renderer), zrzut świeżo załadowanej
     strony, `heading = "Otwórz adres: <url>"`, brak adnotacji celu;
     `prev_cursor := None` (nowy widok).
   - `slide`: brak zrzutu; `GuidePage(kind="slide")` z tytułem/podtytułem/
     notatkami.
   - `wait` / bramka `when` (gate): wykonaj oczekiwanie dla poprawności
     stanu strony, ale **nie** twórz strony PDF — chyba że krok niesie `say`,
     wtedy krótka strona tekstowa bez zrzutu.
3. Tekst po prawej: `page_text(step)` = `step.caption` jeśli ustawione, w
   przeciwnym razie `step.narration()` (`say`/`teach`).
4. `layout.py` buduje HTML; `pdf.py` drukuje do PDF.

## Adnotacje (szczegóły geometrii)

Liczone w `annotate.py` z danych zebranych na żywej stronie, w pikselach
zrzutu (uwzględniając `deviceScaleFactor`). Rysowane jako warstwa `<svg>`
nałożona na `<img>` w HTML (nie wypalane w PNG — łatwe do korekty stylu):

- **arrow**: linia z grotem od `prev_cursor` do `target_center`.
- **click**: czerwone kółko o stałym promieniu wokół `target_center`.
- **typed**: czerwony prostokąt = `target_box` pola po wpisaniu tekstu.
- **hover**: półprzezroczysta poświata/ramka = `target_box`.

Współrzędne skalują się do rozmiaru `<img>` w układzie strony (viewBox SVG =
`screenshot_size`), więc pozostają trafne niezależnie od skali druku.

## Zmiany w modelu i CLI

- `models/scenario.py`: dodać opcjonalne pole `caption: str | None = None`
  do `Step` (jak `say` — nie liczy się do walidatora „dokładnie jedna
  komenda”; `extra="forbid"` wymaga jawnego dodania pola). Wstecznie zgodne.
- `cli.py`: nowa komenda `guide` (wzorowana na `render_cmd`): argumenty
  `path`, `-o/--out`, `--timeout`, `--verbose`; ładuje scenariusz, uruchamia
  Chromium przez `async_playwright()`, woła `run_guide(...)`.
- `pyproject.toml`: bez zmian w zależnościach runtime.

## Obsługa błędów

- Brak `*.compiled.yaml` → czytelny błąd „najpierw `guidebot compile`”
  (spójnie z `render`).
- Nierozwiązany cel / `PendingAction` w skompilowanym pliku → błąd jak w
  renderze (guide nie kompiluje).
- Krok `optional`, którego element nie istnieje na stronie → pomiń stronę
  tego kroku (spójnie z tolerancją renderu dla optional), z ostrzeżeniem.
- `page.pdf()` jest wspierane tylko w Chromium headless — komenda wymusza
  Chromium (jak reszta narzędzia).

## Testy

Jednostkowe (bez sieci, bez ffmpeg):

- `annotate.py`: strzałka/kółko/ramka liczone z zadanych bounding-boxów i
  pozycji kursora; brak strzałki gdy `prev_cursor is None`.
- budowa manifestu: mapowanie `flat_steps` → strony (interaktywne+navigate+
  slide dają strony; wait/gate pomijane; `optional` bez celu pomijany).
- `page_text`: `caption` nadpisuje narrację; fallback do `say`/`teach`.
- `layout.py`: HTML zawiera tyle bloków-stron ile `GuidePage`; warstwa SVG
  ma poprawny `viewBox`.

Integracyjny (oznaczony, jak istniejące e2e):

- `guide` na fiksturze `tests/integration/fixtures/app.html` produkuje
  niepusty PDF; liczba stron zgodna z oczekiwaną (np. przez zliczenie z
  `pdfinfo`, jeśli dostępne, albo długość manifestu zwróconą przez
  `run_guide`).

## Kryteria akceptacji

- `guidebot guide examples/login.scenario.yaml -o /tmp/login.guide.pdf`
  tworzy poprawny PDF landscape.
- Kroki interaktywne mają zrzut z widocznym kursorem i właściwą adnotacją;
  `navigate` ma stronę z nagłówkiem adresu; `slide` ma stronę-przekładkę.
- `wait`/bramki nie tworzą stron.
- Renderer wideo działa bez zmian (żaden istniejący test nie regresuje).
