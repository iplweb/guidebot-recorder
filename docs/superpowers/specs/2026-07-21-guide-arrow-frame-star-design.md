# Przewodnik PDF: strzałka do ramki, ramka celu, gwiazdka kliknięcia

Data: 2026-07-21
Status: projekt zatwierdzony, gotowy do planu wdrożenia

## Problem

Trzy niezależne wady adnotacji na stronach przewodnika PDF:

1. **Strzałka przekreśla pola.** `annotations_for` rysuje odcinek od `prev_cursor`
   (środek poprzedniego celu) do `center` (środek obecnego). Gdy oba cele to pola
   tekstowe na tej samej wysokości, strzałka przechodzi przez połowę każdego z nich —
   wygląda to jak przekreślenie, a nie jak wskazanie.
2. **Kliknięcie nie ma ramki.** `type`, `hover` i `select` obrysowują cel prostokątem;
   `click` dostaje tylko okrąg `r=22` na środku. Kliknięte pole nie jest wyróżnione.
3. **Okrąg kliknięcia myli się z zakreśleniem.** Czerwony okrąg wokół kursora czyta się
   jak elipsa komendy `highlight`, choć znaczy co innego.

## Rozwiązanie w skrócie

- Strzałka jest przycinana z **obu stron** do brzegów kształtów obu celów.
- **Każda** akcja z pudełkiem (`click`, `type`, `hover`, `select`) obrysowuje cel czerwoną
  ramką. `highlight` zostaje przy swojej kolorowej elipsie — bez ramki.
- Kliknięcie oznacza **ośmioramienna gwiazdka** wokół kursora zamiast okręgu.

Decyzje zatwierdzone przez użytkownika: przycięcie obu końców (nie tylko celu), bez
dodatkowego odstępu między grotem a ramką; ramka dla wszystkich akcji z pudełkiem, ale
nie dla `highlight`; gwiazdka ośmioramienna, bez zachowania okręgu.

## Architektura

Zmiana rozkłada się na warstwy zgodnie z istniejącym podziałem pakietu `guide`:

| Warstwa | Plik | Rola po zmianie |
|---|---|---|
| Matematyka przycinania | `guidebot_recorder/guide/geometry.py` (**nowy**) | Wyjście promienia z kształtu, przycięty odcinek strzałki |
| Geometria adnotacji | `guidebot_recorder/guide/annotate.py` | Składa adnotacje; woła przycinanie; wystawia kształt celu |
| Model danych | `guidebot_recorder/guide/model.py` | Nowy zestaw rodzajów adnotacji |
| Rysowanie SVG | `guidebot_recorder/guide/layout.py` | Ramka, gwiazdka, elipsa, strzałka |
| Przebieg scenariusza | `guidebot_recorder/guide/capture.py` | Pamięta kształt poprzedniego celu |

### 1. `guidebot_recorder/guide/geometry.py` — nowy moduł

Czysta matematyka, bez I/O i bez przeglądarki, wzorem `overlay/geometry.py`.
`Ellipse` jest importowana z `overlay/geometry.py`, nie duplikowana — to ten sam
kształt, który film obrysowuje kursorem.

```python
class Rect(NamedTuple):
    x: float
    y: float
    w: float
    h: float

Shape = Rect | Ellipse

def rect_from_box(box: dict) -> Rect:
    """Playwright bounding box (`x`/`y`/`width`/`height`) -> Rect."""

def ray_exit(origin: _Point, toward: _Point, shape: Shape) -> _Point:
    """Punkt, w którym półprosta `origin` -> `toward` opuszcza `shape`.

    Zwraca `origin` bez zmian, gdy `origin` nie leży ściśle wewnątrz kształtu,
    gdy `origin == toward`, albo gdy kształt jest zdegenerowany (zerowy wymiar).
    """

MIN_ARROW = 12.0

def clipped_arrow(
    start: _Point,
    end: _Point,
    *,
    start_shape: Shape | None,
    end_shape: Shape | None,
) -> tuple[_Point, _Point] | None:
    """Odcinek między brzegami obu kształtów, albo `None` gdy się zdegeneruje."""
```

**`ray_exit` dla `Rect`** — metoda slabów. Dla każdej osi liczony jest parametr `t`
trafienia w tę krawędź, w którą kierunek faktycznie idzie (`dx > 0` → prawa krawędź,
`dx < 0` → lewa, `dx == 0` → oś pomijana). Wynik to najmniejsze dodatnie `t`.

**`ray_exit` dla `Ellipse`** — równanie kwadratowe w przestrzeni znormalizowanej
(`u = (ox - cx) / rx`, `v = (oy - cy) / ry`). Rozwiązywane jest
`(u + t·du)² + (v + t·dv)² = 1`; brany jest dodatni pierwiastek. Gdy `rx <= 0`,
`ry <= 0`, `u² + v² >= 1` (punkt poza elipsą) albo współczynnik przy `t²` wynosi zero,
zwracany jest `origin`.

**`clipped_arrow`** składa oba wyjścia i pilnuje degeneracji:

1. `a = ray_exit(start, end, start_shape)` gdy `start_shape` jest podany, inaczej `start`.
2. `b = ray_exit(end, start, end_shape)` gdy `end_shape` jest podany, inaczej `end`.
3. Gdy iloczyn skalarny `(b - a) · (end - start)` jest `<= 0` — kształty zachodzą na
   siebie i przycięty odcinek jest odwrócony → `None`.
4. Gdy `|b - a| < MIN_ARROW` (12 px zrzutu) → `None`.
5. Inaczej `(a, b)`.

Kroki 3 i 4 oba zwracają `None`, więc ich wzajemna kolejność jest behawioralnie
obojętna — test zwrotu stoi pierwszy tylko dlatego, że odwrócony odcinek to bardziej
zaskakujący przypadek. (Kolejność zaczęłaby mieć znaczenie dopiero, gdyby któryś krok
zwracał coś innego niż `None`, np. skrócony odcinek.)

**Próg `MIN_ARROW` obowiązuje zawsze**, również gdy oba kształty są `None`. To świadoma
zmiana zachowania: dziś przewodnik rysuje strzałkę nawet przy kilkupikselowym przeskoku
kursora, a taki grot jest nieczytelny. Wynik `None` przy braku kształtów oznacza więc
„odcinek krótszy niż 12 px", a nie „przycięcie się nie udało".

**Decyzja: brak strzałki zamiast kikuta.** Gdy dwa kolejne cele nachodzą na siebie albo
sąsiadują bliżej niż 12 px, strona nie dostaje strzałki. Kikut lub strzałka wskazująca
wstecz są gorsze niż jej brak — sąsiadujące cele i tak czyta się z ramek.

### 2. `annotate.py` — kształt celu i przycinanie

Nowa funkcja publiczna, wołana zarówno wewnątrz `annotations_for`, jak i przez
`capture.py` przy zapamiętywaniu poprzedniego kroku:

```python
def target_shape(
    action: str,
    *,
    box: dict | None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> Shape | None:
    """Kształt, który adnotacje rysują wokół celu.

    `highlight` -> dopasowana do zrzutu elipsa (ta sama, którą rysuje adnotacja);
    pozostałe akcje z pudełkiem -> `Rect` z pudełka; brak pudełka -> `None`.
    """
```

Dwa przypadki brzegowe, rozstrzygnięte zgodnie z dzisiejszym zachowaniem `annotate.py`:

- `highlight` z pudełkiem, ale **bez `mark`** → `None`. Padding jest nieznany, więc nie
  ma z czego policzyć elipsy; dziś taki krok nie dostaje żadnej adnotacji zakreślenia.
- `bounds=None` → elipsa **bez** `fit_to_bounds`, dokładnie jak w `annotate.py:52-53`.

Funkcja jest czysta i tania, więc dwukrotne wywołanie (raz w `annotations_for`, raz w
`capture.py`) jest tańsze i czytelniejsze niż zwracanie krotki `(adnotacje, kształt)`.

Zmieniona sygnatura:

```python
def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    prev_shape: Shape | None = None,
    center: _Point | None,
    box: dict | None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> list[Annotation]:
```

`prev_shape` ma domyślnie `None`, żeby wywołanie bez niego dawało dokładnie dzisiejsze
zachowanie dla początku strzałki (nieprzycięty start) — ułatwia to testy jednostkowe
skupione na jednym końcu.

Kolejność składania (bez zmian co do zasady: strzałka pod znacznikami):

1. Gdy są `prev_cursor` i `center`: `clipped_arrow(prev_cursor, center,
   start_shape=prev_shape, end_shape=target_shape(...))`. Wynik `None` → brak strzałki.
2. Gdy `action in {"click", "type", "hover", "select"}` i jest `box`: adnotacja `frame`.
3. Gdy `action == "click"` i jest `center`: adnotacja `click` (gwiazdka).
4. Gdy `action == "highlight"`, jest `box` i jest `mark`: adnotacja `highlight` z elipsy
   zwróconej przez `target_shape` (bez powtarzania `ellipse_around` / `fit_to_bounds`).

Stałe: `CLICK_RADIUS` znika, pojawiają się `CLICK_INNER = 16.0` i `CLICK_OUTER = 30.0`
(piksele zrzutu). `CLICK_INNER` jest dobrane tak, by przerwa mieściła 16-pikselowy
ring/disc kursora z `overlay/cursor.js`, więc sam kursor pozostaje czytelny.

### 3. `model.py` — jeden rodzaj ramki zamiast trzech

`typed`, `hover` i `selected` rysowały się identycznie (ta sama klasa `.rect`). Dochodzi
czwarty przypadek — kliknięcie — więc trzy nazwy na jeden kształt zamieniają się w jedną:

```python
kind: Literal["arrow", "click", "frame", "highlight"]
```

Pola: `arrow` używa `x1`/`y1`/`x2`/`y2`; `frame` — `x`/`y`/`w`/`h`; `click` — `cx`/`cy`
plus nowe `r_inner`/`r_outer`; `highlight` — `cx`/`cy`/`rx`/`ry`/`color`. Pole `r` jest
usunięte (nie ma już okręgu).

Utrata rozróżnienia `typed`/`hover`/`selected` jest świadoma: nic go nie konsumowało,
a YAGNI. Gdyby kiedyś ramka miała mieć inny kolor dla wpisywania niż dla hovera, wróci
jako pole `role` na `frame`, nie jako trzy rodzaje.

### 4. `layout.py` — SVG

- `.rect` → `.frame`, ta sama stylistyka (`stroke: #e11`, `stroke-width: 4`,
  `fill: rgba(238,17,17,0.08)`, `rx="4"`).
- `.circle` usunięta; nowa `.star { stroke: #e11; stroke-width: 4; fill: none;
  stroke-linecap: round; }`.
- `click` rysuje **osiem odcinków** co 45°, każdy od promienia `r_inner` do `r_outer`,
  liczonych od `(cx, cy)`. Współrzędne są zaokrąglane do dwóch miejsc po przecinku,
  żeby HTML nie puchł od 17-cyfrowych zmiennoprzecinkowych.
- `arrow` i `highlight` bez zmian.

### 5. `capture.py` — pamięć kształtu

Obok `prev_cursor` pętla trzyma `prev_shape: Shape | None`. Po zbudowaniu strony:

```python
prev_cursor = res.center
prev_shape = target_shape(act, box=res.box, mark=mark, bounds=(float(size[0]), float(size[1])))
```

`prev_shape` jest zerowany razem z `prev_cursor` w każdym miejscu, które dziś zeruje
kursor (`navigate`, `scroll`) — inaczej po nawigacji strzałka byłaby przycinana do
kształtu z poprzedniej strony.

## Dokumentacja

Legenda adnotacji żyje w czterech plikach — pełna w `docs/pl/pdf-guide.md` i
`docs/en/pdf-guide.md`, skrócona w `docs/pl/cli-reference.md:145`
(„strzałkami, kołami, ramkami, glow") i `docs/en/cli-reference.md:204`
(„arrows, circles, frames, glows"). Po zmianie nie ma ani koła, ani żadnego „glow",
więc obie skrócone wzmianki też się aktualizują — na „strzałkami, ramkami i gwiazdkami"
oraz „arrows, frames and click stars".

Pełna legenda opisuje dzisiejszy
stan i częściowo mija się z kodem już teraz (strzałka jest prosta, nie „zakrzywiona";
hover nie ma żadnej poświaty, tylko tę samą ramkę). Obie legendy dostają wersję zgodną
z kodem po zmianie:

- **Strzałka** — ruch kursora z poprzedniego celu do obecnego; biegnie **między ramkami**,
  nie przez ich środki. Przy celach nachodzących na siebie strzałki nie ma.
- **Czerwona ramka** — cel akcji: kliknięcia, wpisywania tekstu, najechania lub wyboru
  z listy.
- **Gwiazdka** — miejsce kliknięcia myszą, ośmioramienna, wokół kursora.
- **Elipsa** — zakreślenie z kroku `highlight`, w kolorze ze scenariusza (bez zmian).

## Testy

Nowe i zmienione testy jednostkowe (projekt nie ma testów renderujących sam PDF do
porównania pikseli — sprawdzany jest HTML i geometria):

`tests/unit/guide/test_geometry.py` (**nowy**)

- `ray_exit` z prostokąta w czterech kierunkach osiowych i po skosie — punkt leży na
  właściwej krawędzi.
- `ray_exit` z elipsy — punkt spełnia równanie elipsy z tolerancją.
- `ray_exit` zwraca `origin`, gdy punkt jest poza kształtem, gdy `origin == toward`
  i gdy kształt ma zerowy wymiar.
- `clipped_arrow` dla dwóch rozłącznych prostokątów — końce na brzegach, kierunek
  zachowany.
- `clipped_arrow` zwraca `None`, gdy prostokąty nachodzą na siebie.
- `clipped_arrow` zwraca `None`, gdy odstęp między kształtami jest mniejszy niż
  `MIN_ARROW`.
- `clipped_arrow` bez kształtów zwraca oryginalny odcinek — pod warunkiem, że jest
  dłuższy niż `MIN_ARROW`; krótszy daje `None` także bez kształtów.

`tests/unit/guide/test_annotate.py` (zmiany)

- `click` daje `frame` **i** `click`; gwiazdka ma `r_inner`/`r_outer`, nie `r`.
- `type`, `hover`, `select` dają `frame` (zamiast `typed`/`hover`/`selected`).
- Strzałka między dwoma polami kończy się na brzegu ramki celu, nie na jego środku
  (asercja: `x2` różne od `center[0]` i punkt leży na krawędzi pudełka).
- Strzałka zaczyna się na brzegu `prev_shape`, gdy ten jest podany.
- Brak `prev_shape` → start nieprzycięty (zgodność wsteczna sygnatury).
- Nachodzące cele → brak adnotacji `arrow`.
- `target_shape` zwraca `Rect` dla akcji z pudełkiem, `Ellipse` dla `highlight`
  i `None` bez pudełka.

`tests/unit/guide/test_layout.py` (zmiany — to tu, a nie w `test_pdf.py`, żyją asercje
SVG; `test_pdf.py` testuje wyłącznie `html_to_pdf` i nie zmienia się wcale)

- `test_screenshot_page_has_svg_viewbox_and_circle` i sąsiedni test budują
  `Annotation(kind="click", …, r=22.0)` i asertują `<circle` — jedno i drugie znika.
  Po zmianie: konstrukcja z `r_inner`/`r_outer`, asercja ośmiu `<line class="star"`
  i braku `<circle class="circle"`.
- SVG dla `type`/`hover`/`select`/`click` zawiera `<rect class="frame"`.

Testy pominięte w pierwszej wersji tego spec-u, a łamane przez zmianę modelu:

- `tests/unit/guide/test_model.py:19` — `Annotation(kind="click", …, r=18.0)`;
  pole `r` znika, konstrukcja przechodzi na `r_inner`/`r_outer`.
- `tests/unit/guide/test_capture.py:310` — `assert any(a.kind == "selected" …)`;
  po zwinięciu rodzajów staje się `"frame"`. Ten sam test zyskuje asercję, że
  `prev_shape` jest przekazywany do kolejnego kroku.
- `tests/integration/test_guide.py:372` — `assert any(annotation.kind == "selected" …)`;
  również przechodzi na `"frame"`.

Wbrew pierwotnemu twierdzeniu tego spec-u testy integracyjne **nie są** obojętne:
`test_guide.py` asertuje rodzaj adnotacji i wymaga zmiany. Pozostałe
(`tests/integration/test_compile_render.py` i pokrewne) rzeczywiście nie — sprawdzają
istnienie i strukturę PDF-a, nie kształt znaczników.

`tests/unit/guide/test_highlight.py` woła `annotations_for` wyłącznie argumentami
nazwanymi, więc dodanie `prev_shape` z wartością domyślną go nie łamie; dochodzi tam
jeden test, że dla kroku `highlight` strzałka jest przycinana do **elipsy**, nie do
pudełka.

## Zakres wyłączony

- Krzywa (łuk) zamiast prostej strzałki — osobny temat, film ma własny łuk kursora.
- Odstęp między grotem a ramką — świadomie odrzucony przy zatwierdzaniu projektu.
- Różne kolory ramek per akcja — YAGNI, patrz uzasadnienie przy `model.py`.
- Zmiany w filmie (`recorder/render.py`, `overlay/cursor.js`) — ta zmiana dotyczy
  wyłącznie przewodnika PDF.
