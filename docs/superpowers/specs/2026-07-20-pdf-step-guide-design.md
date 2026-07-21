# PDF krok-po-kroku (`guidebot guide`) — projekt

Data: 2026-07-20
Gałąź: `feat/pdf-step-guide`
Status: zatwierdzony projekt (spec); **decyzja S4 wycofana 2026-07-21**

> **Errata 2026-07-21 — decyzja S4 („faza PDF zawsze headless") jest wycofana.**
> Przesłanka, na której się opierała — „`page.pdf()` rzuca w trybie headed" —
> została obalona empirycznie. Oryginalne sformułowania zostają w dokumencie
> nietknięte; miejsca, których to dotyczy (architektura `pdf.py`, sekcja
> „Kontekst przechwytywania (S1)", zmiany w CLI, obsługa błędów), są oznaczone
> adnotacją **[Wycofane 2026-07-21]**, a pełne wyjaśnienie z wersjami znajduje
> się przy S4 w sekcji „Kontekst przechwytywania (S1)". Następca:
> `2026-07-21-guide-headed-design.md`.

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

- `capture.py` — przebieg przechwytywania: buduje kontekst jak renderer,
  odtwarza skompilowany scenariusz, dla każdego kroku ustawia kursor przez
  `Recorder`, robi `page.screenshot()` i produkuje `GuidePage` z geometrią
  adnotacji.
- `annotate.py` — czysta geometria adnotacji: z bounding-boxów i pozycji
  kursora liczy współrzędne strzałki / kółka / ramki (bez I/O, testowalne
  jednostkowo).
- `layout.py` — składa listę `GuidePage` w jeden dokument HTML (CSS grid:
  panel zrzutu z warstwą SVG po lewej, tekst po prawej; jedna strona
  landscape na krok).
- `pdf.py` — renderuje HTML do PDF przez Chromium `page.pdf(...)` (zawsze
  headless, `landscape=True`, `print_background=True`).
  **[Wycofane 2026-07-21]** „zawsze headless" nie obowiązuje — `page.pdf()`
  działa również w trybie headed; zostają `landscape=True` i
  `print_background=True`. Patrz errata przy S4 poniżej.
- `guide.py` — publiczne wejście `run_guide(...)`, spina powyższe; wywoływane
  z CLI.

### Wymagana zmiana w `Recorder` (blocker B1)

`_point_and_prepare` jest prywatne, nie zwraca geometrii, a wołanie go i
potem `recorder.click(...)` uruchamia glide/ripple dwukrotnie. Dodajemy
publiczne API i przebudowujemy prywatne na nim:

```python
class PointResult(NamedTuple):
    locator: Locator
    box: dict | None            # bounding_box() celu (x,y,width,height) lub None
    center: tuple[float, float] | None

async def point(self, target, *, ripple: bool = True) -> PointResult: ...
```

`guide` woła `point(target, ripple=False)` (bez pierścienia — S2), robi zrzut,
a następnie odpala surową akcję na `result.locator` (`locator.click()` /
`locator.fill(...)`), a nie ponownie przez `recorder.click`. Gdy `box is None`
(brak bounding-boxa) — pomiń adnotacje celu z ostrzeżeniem, nie wywracaj się.

Model danych (wewnętrzny, nie serializowany na dysk):

```python
@dataclass
class Annotation:
    kind: Literal["arrow", "click", "typed", "hover"]
    # jawne, opcjonalne pola per rodzaj (łatwiejsze do sprawdzania typów niż
    # jeden NamedTuple z payloadem zależnym od kind):
    x1: float | None = None; y1: float | None = None    # arrow: początek
    x2: float | None = None; y2: float | None = None    # arrow: koniec (grot)
    cx: float | None = None; cy: float | None = None; r: float | None = None  # click
    x: float | None = None; y: float | None = None; w: float | None = None; h: float | None = None  # typed/hover

@dataclass
class GuidePage:
    kind: Literal["step", "navigate", "slide", "text"]
    screenshot: Path | None      # None dla slide i text (przekładka / sam say)
    text: str                    # opis po prawej
    heading: str | None          # np. "Otwórz adres X", tytuł slajdu
    annotations: list[Annotation]
    screenshot_size: tuple[int, int] | None  # do viewBox SVG; None bez zrzutu
```

## Przepływ danych i sterowanie (parzystość z rendererem)

1. `run_guide(path, out_pdf, browser, *, env, timeout, verbose)` ładuje
   `Scenario` i `CompiledScenario` i buduje `flat = scenario.flat_steps()`
   zipowane **`strict=True`** z `compiled.actions` (jak `run_render`,
   render.py:1655-1667). Prolog: `PendingAction` na kroku **obowiązkowym,
   spoza gałęzi** → twardy błąd „uruchom `compile` / `compile --force`”
   (render.py:1668-1673). Pending na bramce/`optional` jest legalne i
   obsługiwane niżej (B3).
2. `capture.py` iteruje `flat`, utrzymując `prev_cursor: (x,y)|None` oraz
   `skipped_branch: int|None`. Dyspozycja akcji idzie po **`cached.action`**
   (`click`/`hover`/`type`/`waitFor`), **nie** po `step.command_kind()` —
   `teach` zamraża się jako click/hover/type (B2; resolution.py:95-99,
   render.py:2391-2471). Per `FlatStep(step, branch, is_gate)`:
   - Jeśli `skipped_branch is not None` i `branch == skipped_branch` → pomiń
     (żadnej strony); reset `skipped_branch` gdy wracamy poza gałąź (B4;
     render.py:1862-1884, 2120-2122).
   - **gate** (`is_gate`): wykonaj `waitFor`; jeśli element nieobecny
     (timeout) lub akcja pozostała `PendingAction` bez reasonera → ustaw
     `skipped_branch = branch`, pomiń całą gałąź (B4). Bramka nie tworzy
     strony.
   - **navigate**: wykonaj nawigację (`_resolve_url`, render.py:1474-1478),
     zrzut świeżo załadowanej strony; `heading = "Otwórz adres: <resolved>"`;
     brak adnotacji celu; `prev_cursor := None`.
   - **slide**: bez zrzutu; `GuidePage(kind="slide")` z tytułem/podtytułem/
     notatkami.
   - **akcja z celem** (`click`/`hover`/`type`): dla obowiązkowego kroku
     sprawdź tożsamość jak renderer (`reuse_is_valid`, render.py:2384-2385);
     niezgodność → błąd „uruchom `compile --force`” (S5). Krok `optional`,
     którego celu brak → pomiń stronę z ostrzeżeniem (B3/optional).
     `res = await recorder.point(target, ripple=False)`. Kolejność ma
     znaczenie (S3):
     - `click`/`hover`: **najpierw zrzut**, potem `res.locator.click()` /
       `.hover()` (po klliknięciu strona może nawigować, cel znika).
     - `type`: `res.locator.fill(text)` (natychmiast, `type_delay_ms=None`),
       **potem zrzut** (ramka wokół wpisanego pola).
     Po akcji: `recorder.apply_readiness(cached.expect)` jak renderer
     (render.py:2483-2498), by następny locator nie ścigał się z nawigacją.
     Adnotacje z `res`: `arrow` `prev_cursor → res.center` (jeśli
     `prev_cursor` i `res.center`); `click` (kółko) dla click; `hover`
     (poświata) dla hover; `typed` (ramka=`res.box`) dla type.
     `prev_cursor := res.center`.
   - **`say`-only** (brak komendy, jest `say`) oraz **`wait` z `say`**:
     `GuidePage(kind="text")` — strona tekstowa bez zrzutu (B5).
   - **`wait` bez `say`**: wykonaj oczekiwanie, żadnej strony.
   - **popup** (`cached.opens_popup`): w v1 **twardy błąd** na starcie
     (prolog skanuje akcje) — „scenariusze z popupem nieobsługiwane w
     `guide` v1” (B6; render.py:2387-2464). Bez cichego wejścia w złe okno.
3. Tekst po prawej: `page_text(step)` = `step.caption` jeśli ustawione, inaczej
   `step.narration()` (`say`/`teach`).
4. `layout.py` buduje HTML; `pdf.py` drukuje do PDF.

## Kontekst przechwytywania (S1)

`guide` buduje kontekst **tak jak renderer** (render.py:1705-1734), bo geometria
i `reuse_is_valid` zależą od tego samego układu, w jakim kompilowano:
`viewport`/`locale` z `Config`, a przy `chrome.enabled` — powłoka macOS-bar i
sterowanie stroną przez `Recorder(frame=site_frame)`. To znaczy, że dla
scenariuszy z chrome PDF pokaże pasek przeglądarki (akceptowane, spójne z
wideo). `bounding_box()` jest względem viewportu głównej ramki, więc
współrzędne adnotacji pozostają poprawne w obu trybach. Jeśli da się to zrobić
czysto, wspólną budowę kontekstu wydzielamy do helpera używanego i przez
render, i przez guide; jeśli nie — replikujemy wiernie w `capture.py`.

Faza PDF (`pdf.py`) **zawsze** działa w headless (bo `page.pdf()` rzuca w
trybie headed, cli.py:212) — niezależnie od ewentualnej flagi debug w capture.

> **[Wycofane 2026-07-21 — decyzja S4]** Powyższa przesłanka jest nieprawdziwa.
> `page.pdf()` **nie** rzuca w trybie headed. Reguła pochodziła z czasów, gdy
> Chromium miał osobną implementację trybu headless; „new headless" ujednolicił
> ścieżki kodu i `Page.printToPDF` jest dostępne w obu trybach. Zweryfikowano
> empirycznie 2026-07-21 na Playwright 1.61.0 / Chromium 149.0.7827.55 /
> macOS (darwin 25.5.0):
>
> | Sprawdzenie | Wynik |
> | --- | --- |
> | `page.pdf()` na `headless=False`, `set_content` | OK, poprawny PDF (`%PDF-1.4`) |
> | `guide.pdf.html_to_pdf()` na `headless=False` (`file://`, `goto(wait_until="load")`, `landscape=True`, `print_background=True`) | OK, 13 810 bajtów |
>
> **Konsekwencja: decyzja S4 zostaje wycofana, a nie zrealizowana.** Nie jest
> potrzebna druga instancja przeglądarki ani rozdzielenie fazy przechwytywania
> od fazy druku — wystarczy jedna przeglądarka uruchomiona z
> `headless=not headed`, jak w pozostałych komendach. `guidebot guide` dostaje
> flagi `--headed` i `--pause-on-error`; szczegóły w
> `2026-07-21-guide-headed-design.md`.

## Adnotacje (szczegóły geometrii)

Liczone w `annotate.py` z danych zebranych na żywej stronie, w pikselach
zrzutu (uwzględniając `deviceScaleFactor`). Rysowane jako warstwa `<svg>`
nałożona na `<img>` w HTML (nie wypalane w PNG — łatwe do korekty stylu):

- **arrow**: linia z grotem od `prev_cursor` do `res.center`.
- **click**: czerwone kółko o stałym promieniu wokół `res.center`.
- **typed**: czerwony prostokąt = `res.box` pola po wpisaniu tekstu.
- **hover**: półprzezroczysta poświata/ramka = `res.box`.

Współrzędne skalują się do rozmiaru `<img>` w układzie strony (viewBox SVG =
`screenshot_size`), więc pozostają trafne niezależnie od skali druku.

## Zmiany w modelu i CLI

- `models/scenario.py`: dodać opcjonalne pole `caption: str | None = None`
  do `Step` (jak `say` — nie liczy się do walidatora „dokładnie jedna
  komenda”; `extra="forbid"` wymaga jawnego dodania pola). Wstecznie zgodne
  (potwierdzone w review: `_exactly_one_command`, walidator tłumaczeń i
  `config_hash` nie ruszają `caption`). Krok z **samym** `caption` (bez
  komendy i bez `say`) nadal jest błędem „pusty krok” — świadomie.
- `recorder/recorder.py`: publiczne `point(target, *, ripple=True)` +
  `PointResult` (B1); `_point_and_prepare` przebudowane na `point`.
- `cli.py`: nowa komenda `guide` (wzorowana na `render_cmd`): argumenty
  `path`, `-o/--out`, `--timeout`, `--verbose`; ładuje scenariusz, uruchamia
  Chromium przez `async_playwright()`, woła `run_guide(...)`. Faza PDF wymusza
  headless niezależnie od flag (S4).
  **[Wycofane 2026-07-21]** S4 wycofana: faza PDF niczego nie wymusza, a `guide`
  ma flagi `--headed` i `--pause-on-error` jak `compile`/`render`
  (`launch(headless=not headed)`). Patrz errata przy S4 wyżej.
- `pyproject.toml`: bez zmian w zależnościach runtime.

## Obsługa błędów

- Brak `*.compiled.yaml` → czytelny błąd „najpierw `guidebot compile`”
  (spójnie z `render`, render.py:1643-1651).
- `PendingAction` na kroku obowiązkowym spoza gałęzi → twardy błąd (prolog).
  Pending na bramce/`optional` → pomiń gałąź/krok z ostrzeżeniem (B3).
- Bramka `when` nieobecna → pomiń **całą** gałąź (`skipped_branch`, B4).
- Krok `optional` bez celu → pomiń jego stronę z ostrzeżeniem.
- Niezgodność tożsamości celu obowiązkowego (`reuse_is_valid`) → błąd
  „uruchom `compile --force`” (S5).
- `cached.opens_popup` w którejkolwiek akcji → twardy błąd v1 (B6).
- Faza `page.pdf()` zawsze headless (S4).
  **[Wycofane 2026-07-21]** — `page.pdf()` działa w obu trybach, to nie jest
  warunek poprawności ani przypadek błędny. Patrz errata przy S4 wyżej.

## Uwagi (z review)

- Wpisywany tekst może pochodzić z sekretów (`scenario_sensitive_values`,
  render.py:1626) i zostanie zamrożony na zrzucie — **akceptowane ryzyko** w
  v1; maskowanie pól wrażliwych to ewentualne v2.
- `navigate` heading pokazuje **rozwiązany** URL (`_resolve_url`), nie surowy.

## Testy

Jednostkowe (bez sieci, bez ffmpeg):

- `annotate.py`: strzałka/kółko/ramka liczone z zadanych bounding-boxów i
  pozycji kursora; brak strzałki gdy `prev_cursor is None`; adnotacja idzie
  po `cached.action` (`type` → `typed`, nie po `command_kind`).
- budowa manifestu: mapowanie `flat_steps` → strony — akcja z celem, navigate,
  slide, `say`-only/`wait`+`say` dają strony; `wait` bez `say` i bramki nie;
  nieobecna bramka pomija całą gałąź (`skipped_branch`); `optional` bez celu
  pomijany; `type` z kroku `teach` daje adnotację `typed`, nie `click`.
- prolog: `opens_popup` → wyjątek; `PendingAction` obowiązkowy → wyjątek;
  pending na bramce/`optional` → brak wyjątku.
- `page_text`: `caption` nadpisuje narrację; fallback do `say`/`teach`.
- `Recorder.point`: zwraca `locator`+`box`+`center`; `ripple=False` nie woła
  ripple; `box is None` → `center is None` bez wyjątku.
- `layout.py`: HTML zawiera tyle bloków-stron ile `GuidePage`; warstwa SVG
  ma poprawny `viewBox`.

Integracyjny (oznaczony, jak istniejące e2e):

- `guide` na fiksturze `tests/integration/fixtures/app.html` (compile+guide)
  produkuje niepusty PDF; liczba stron zgodna z długością manifestu z
  `run_guide`.
- fikstura z bramką `when`, której element nie występuje → strony gałęzi
  pominięte.

## Kryteria akceptacji

- `guidebot guide examples/login.scenario.yaml -o /tmp/login.guide.pdf`
  tworzy poprawny PDF landscape.
- Kroki z celem mają zrzut z widocznym kursorem i adnotacją zgodną z
  `cached.action` (click→kółko, type→ramka, hover→poświata); `navigate` ma
  stronę z rozwiązanym adresem; `slide` ma stronę-przekładkę; `say`-only ma
  stronę tekstową.
- `wait`/bramki nie tworzą stron; nieobecna bramka pomija całą gałąź.
- Scenariusz z popupem kończy się jasnym błędem v1.
- Renderer wideo działa bez zmian (żaden istniejący test nie regresuje;
  `point()` to czysty refaktor `_point_and_prepare`).
