# Komenda `highlight` — zakreślanie kontrolki lub obszaru

Data: 2026-07-21
Status: zatwierdzony projekt po przeglądzie adwersaryjnym, gotowy do wdrożenia

## Problem

Scenariusz potrafi kliknąć, najechać, wpisać tekst i przewinąć stronę, ale nie
potrafi **wskazać** elementu bez interakcji z nim. Narrator mówi „tutaj pojawiają
się wyniki", a widz nie wie, na którą część ekranu patrzeć. Kliknięcie w tabelę
albo najechanie na nią zmienia stan strony, więc żadna istniejąca komenda nie
zastąpi wskazania.

## Rozwiązanie w skrócie

Nowa komenda `highlight` z celem semantycznym. W filmie kursor okrąża element po
elipsie, zostawiając za sobą narastający ślad „zakreślacza"; w PDF-owym
przewodniku ten sam element dostaje elipsę na zrzucie ekranu. Komenda **nie
dotyka strony** — żadnego kliknięcia, najechania ani zdarzenia DOM.

## Język scenariusza

```yaml
# skrót — sam cel
- highlight: "przycisk Zapisz"

# postać pełna
- highlight:
    what: "tabela z wynikami"
    padding: 12        # px wokół prostokąta elementu
    loops: 3           # liczba okrążeń kursora
    hold: 1.5          # s, przez które ślad zostaje po ostatnim okrążeniu
    color: "#22c55e"   # kolor śladu i elipsy w PDF
  say: "Tutaj pojawiają się wyniki wyszukiwania"
```

`highlight` jest komendą **z celem**, więc działa z `optional: true`, wewnątrz
bloku `when:`, z `caption`, `say` i `translations` — bez wyjątków w regułach
walidacji kroku.

### Model kroku

```python
class Highlight(BaseModel):
    """Zakreślenie celu — pola opcjonalne dziedziczą z ``config.highlight``."""

    model_config = ConfigDict(extra="forbid")
    what: str
    padding: float | None = Field(default=None, ge=0)
    loops: int | None = Field(default=None, ge=1, le=5)
    hold: float | None = Field(default=None, ge=0)
    color: str | None = None

    @field_validator("what")
    @classmethod
    def _what_is_not_blank(cls, value: str) -> str: ...   # pusty/białe znaki → błąd

    def resolved(self, defaults: HighlightConfig) -> ResolvedHighlight:
        """Scal krok z domyślnymi z configu — jedyne miejsce, gdzie to zachodzi."""
```

`ResolvedHighlight` to zamrożony `NamedTuple` `(what, padding, loops, hold,
color)` bez pól opcjonalnych. Zarówno `render`, jak i `guide` wołają
`resolved()` — dzięki temu reguła dziedziczenia istnieje w kodzie raz i daje się
przetestować bez przeglądarki.

Skrót tekstowy normalizuje **walidator `mode="before"` na polu `Step.highlight`**
(`"tekst"` → `{"what": "tekst"}`), a nie leniwa metoda na wzór
`Step.scroll_config()`. Różnica jest celowa: `scroll_config()` normalizuje dopiero
przy użyciu i niczego nie waliduje, a `highlight: "   "` ma być błędem **przy
wczytywaniu pliku**, gdzie diagnostyka umie pokazać `plik:linia` i fragment YAML-a.
`Step.highlight_config()` istnieje jako wygodny getter zwracający `Highlight`, ale
normalizacja zaszła już wcześniej.

Zmiany w `models/scenario.py`: `PRIMARY_COMMANDS` zyskuje `"highlight"`,
`requires_target()` zwraca dla niego `True`, pole `highlight: str | Highlight | None`
trafia do `Step`.

### Domyślne wartości (`config.highlight`)

```python
class HighlightConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str = "rgba(250,204,21,.85)"          # żółty zakreślacz
    padding: float = Field(default=8.0, ge=0)
    loops: int = Field(default=2, ge=1, le=5)
    hold: float = Field(default=0.6, ge=0)
```

`HighlightConfig` **nie wchodzi** do `config_hash()`: ta funkcja jest wąską
projekcją (viewport, locale, `tts.lang`, geometria chrome), a nie hashem całego
configu — dodanie pola nie unieważnia żadnego istniejącego `*.compiled.yaml`.
Pilnuje tego **istniejący** złoty test `GOLDEN_MINIMAL_HASH`
(`tests/unit/models/test_config_setup.py:120`), który musi przejść bez zmiany
oczekiwanej wartości.

`color` jest dowolnym łańcuchem, przekazywanym do CSS bez interpretacji. Do
przeglądarki trafia jako element ładunku JSON w `page.evaluate`, nigdy przez
sklejanie źródła JS; do PDF-a trafia przez `html.escape`. Nie jest więc wektorem
wstrzyknięcia w żadnej z warstw.

## Ścieżka compile → cache

1. `ActionKind` (models/action.py) zyskuje wariant `"highlight"`.
2. **Słownik Reasonera przestaje być pochodną `ActionKind`.** Dziś
   `resolver/reasoner.py:29` robi `_ACTIONS = frozenset(get_args(ActionKind))`
   i wstawia ten zbiór do JSON-schematu odpowiedzi LLM (`reasoner.py:196`).
   Bez zmiany model dostałby `highlight` jako legalną akcję do **wywnioskowania**
   dla kroku `teach` i mógłby zamrozić `CachedAction(action="highlight")` dla
   kroku, który nie ma pól `padding/loops/hold/color`. Dlatego w `models/action.py`
   powstaje jawna stała:

   ```python
   #: akcje, które Reasoner wolno *wywnioskować*; `highlight` wybiera autor
   #: scenariusza, a nie model — to nie jest ten sam zbiór co ``ActionKind``
   REASONER_ACTIONS: tuple[ActionKind, ...] = ("click", "hover", "type", "waitFor", "select")
   ```

   `reasoner.py` używa jej zamiast `get_args(ActionKind)`. Dodatkowo
   `resolution.action_for("teach", resolved)` odrzuca `resolved == "highlight"`
   jasnym błędem — pas i szelki, bo to jedyna ścieżka, w której o akcji decyduje
   model, a nie plik scenariusza.
3. `resolution.step_instruction()` dla `highlight` zwraca `what`;
   `resolution.action_for()` mapuje `"highlight" → "highlight"`.
3a. **Zbiór kandydatów rozszerzony o kontenery — tylko dla tego kroku.**
   `page_context.CANDIDATE_ROLES` to same elementy interaktywne plus `heading`;
   `table`, `form`, `region`, `list`, `figure`, `group`, `article`, `grid` i `img`
   nie trafiały do Reasonera w ogóle. Bez tego `highlight: "tabela z wynikami"` —
   sztandarowy przykład tej komendy — nie miałby czego wskazać i kończyłby się
   `no_action`. Nowa stała `HIGHLIGHT_CANDIDATE_ROLES` dokłada te role, a
   `candidate_roles_for(kind)` wybiera zbiór; `collect_candidates` przyjmuje go
   parametrem. Rozszerzenie jest zawężone do `highlight` świadomie: kontener nie
   jest klikalny, więc w zbiorze `click`/`type`/`select` nie ma czego szukać, a
   poszerzenie go globalnie zmieniłoby wybory modelu w scenariuszach, które już
   się kompilują.
4. `resolver/validate.py` dopuszcza nową akcję na białej liście i sprawdza dla
   niej **tylko** istnienie, unikalność i widoczność celu — bez testów
   edytowalności i typu elementu, bo nic nie klikamy ani nie wpisujemy.
5. `render.py` — mapa `expected_action` zyskuje `"highlight": "highlight"`.
6. `_compiled_from(step)` dla `highlight` to samo `what`. `padding`, `loops`,
   `hold` i `color` są czysto wizualne, więc ich zmiana **nie** wymusza
   ponownego `compile` — dokładnie jak `amount` w kroku `scroll`.
7. `COMPILER_VERSION` **nie** rośnie: nowy wariant `ActionKind` nie zmienia
   znaczenia żadnego już zamrożonego wpisu.
8. `compile.py` — `_compile_step` dostaje **jawną** gałąź `highlight`, która nie
   wykonuje niczego na stronie (komentarz mówi, że to celowy no-op, nie
   przeoczenie). Bez niej krok przeleciałby cicho przez łańcuch `elif`, dając
   przypadkiem poprawne zachowanie z niepoprawnego powodu. `_short()`
   (`compile.py:88`) zyskuje `highlight`, żeby log `--verbose` nie pokazywał
   pustego opisu kroku.

`expect` dla kroku `highlight` to zawsze `none` — komenda nie może wywołać
nawigacji, bo nie dotyka strony. Krok nigdy nie ustawia `opens_popup`; walidator
`CachedAction` już dziś odrzuca `opens_popup` dla akcji innej niż `click`.

## Geometria — jedno źródło prawdy

Moduł `guidebot_recorder/overlay/geometry.py`, czysty (bez I/O, bez przeglądarki):

```python
def ellipse_around(box: dict, padding: float) -> Ellipse:
    """Najmniejsza elipsa o proporcjach prostokąta, która go w całości zawiera."""
    # cx = x + w/2 ; cy = y + h/2
    # rx = (w/2 + padding) * √2 ; ry = (h/2 + padding) * √2

def fit_to_bounds(e: Ellipse, width: float, height: float, margin: float = 4.0) -> Ellipse:
    """Zmieść elipsę w kadrze: najpierw przytnij promienie, potem przesuń środek."""

def ellipse_perimeter(e: Ellipse) -> float:
    """Obwód wg przybliżenia Ramanujana — do wyliczenia czasu okrążenia."""
```

Współczynnik √2 daje najmniejszą elipsę o proporcjach prostokąta, która ten
prostokąt w całości zawiera. Dla szerokiej tabeli elipsa wychodzi wyraźnie
większa od elementu — to świadomy kompromis zatwierdzony przy projekcie: element
jest w środku w całości, a nie przycięty w rogach.

`fit_to_bounds` istnieje, bo bez niego elipsa wokół szerokiego elementu wyjeżdża
poza kadr: w filmie kursor odjechałby poza viewport, a w PDF-ie `.shot`
(`layout.py:17`, `overflow: hidden`) uciąłby elipsę po bokach. Reguła: najpierw
przytnij `rx`/`ry` do połowy kadru minus margines, potem przesuń środek tak, by
elipsa się zmieściła. Gdy element sam jest większy od kadru, elipsa go przetnie —
to akceptowany przypadek brzegowy.

Z modułu korzystają **obie** warstwy: `Recorder.highlight()` (film, kadr =
viewport) i `guide/annotate.py` (PDF, kadr = rozmiar zrzutu). Python liczy
`rx`/`ry` i podaje je JavaScriptowi — inaczej stała √2 zdublowałaby się w Pythonie
i w `cursor.js` i rozjechała przy pierwszej zmianie.

## Film (`render`)

`Recorder.highlight(target, spec: ResolvedHighlight)`:

1. `point(target, ripple=False)` — kursor dolatuje łukiem do środka elementu, bez
   pierścienia kliknięcia i bez dźwięku. Zwraca `box`.
2. Gdy `box is None` — koniec kroku: żadnej animacji, żadnego błędu. To gałąź
   **defensywna, a nie ścieżka degradacji**: każda droga tutaj przechodzi
   walidację odrzucającą `not_visible`, a element widoczny ma prostokąt. Nie ma
   komunikatu `--verbose`, bo `_render_step` tej flagi nie dostaje (ma ją dopiero
   pętla nadrzędna), a przewlekanie jej przez sygnaturę dla gałęzi nieosiągalnej
   w praktyce kosztuje więcej, niż daje. `Recorder.highlight()` woła **wyłącznie
   `render`**, a tam nakładka jest zawsze obecna (`compile` nie wykonuje akcji dla
   tego kroku, a `guide` ma własną, nieanimowaną ścieżkę — patrz niżej).
3. Geometria: `ellipse_around(box, spec.padding)` → `fit_to_bounds(…, viewport)`.
4. `overlay.move_to(page, cx + rx, cy)` — dolot ze środka elementu do **punktu
   wejścia** na elipsę (prawy skraj, kąt 0). Świadomie używamy istniejącego
   `move_to`, a nie nowego kodu w JS: dostajemy za darmo łuk, wyliczanie czasu
   i aktualizację `Overlay.pos`. Bez tego kroku kursor teleportowałby się o `rx`
   pikseli (przy tabeli — o kilkaset).
5. `overlay.encircle(page, cx, cy, rx, ry, loops, hold, color, ms_per_loop)` →
   nowa funkcja `encircle()` w `cursor.js`:
   - kursor jedzie po elipsie zgodnie z ruchem wskazówek zegara zadaną liczbę
     okrążeń, startując i kończąc w punkcie wejścia (całkowita liczba okrążeń),
   - za kursorem narasta ślad: `<ellipse>` w warstwie nakładki ze
     `stroke-dasharray`/`stroke-dashoffset` przesuwanym proporcjonalnie do
     postępu pierwszego okrążenia; kolejne okrążenia obrysowują już narysowany
     ślad,
   - po ostatnim okrążeniu ślad stoi `hold` sekund i gaśnie; sprzątanie idzie
     istniejącym `removeAfterAnimation()` (zdarzenia `finish`/`cancel` plus
     zapasowy `setTimeout`), więc element nie zostaje w DOM,
   - pętla rAF powiela wzorzec `moveTo()` co do joty: postęp liczony z zegara
     (nie z liczby klatek), rejestracja w `state.raf`, współpraca z `cancelMove()`
     i **zapasowy `setTimeout`** rozstrzygający obietnicę, gdy dokument jest
     zbackgroundowany. Bez tego ostatniego render potrafi zawisnąć na zawsze —
     `cursor.js:442` istnieje właśnie z tego powodu.
6. Po powrocie `Overlay.pos` zostaje w punkcie wejścia (kursor tam skończył), więc
   następny krok liczy łuk od prawdziwej pozycji.

**Czas okrążenia** liczy Python: `ms_per_loop = obwód / cursor.speed`, ograniczony
własnymi, nazwanymi stałymi (`ENCIRCLE_MIN_MS = 600`, `ENCIRCLE_MAX_MS = 2600`).
Świadomie **nie** używamy `_glide_duration()`: jego klamry `[min_duration=320,
max_duration=1400]` są dobrane do dolotu po prostej, a obwód elipsy wokół szerokiej
tabeli sięga kilku tysięcy pikseli — trafiłby w sufit i duży obszar okrążałby się
*szybciej* niż mały, odwrotnie do intencji.

### Kontrakt API nakładki

`encircle` trafia do publicznego API kursora, więc **obie** kopie listy nazw
muszą go wymienić:

- `overlay.py:15` — `_API_IS_READY` (kontrola gotowości po stronie Pythona),
- `cursor.js:46-55` — guard „poprzednia wersja API już jest", który przy trafieniu
  robi wczesny `return`. Rozszerzamy listę **i** podbijamy `API_VERSION`, bo
  starsza wstrzyknięta wersja skryptu przechodzi ten guard i zostawiłaby stronę
  bez `encircle` — awaria wyłącznie po nawigacji SPA, czyli najtrudniejsza do
  odtworzenia.

Istniejąca funkcja `cursor.js:528 highlight(x, y, w, h)` (pulsujący prostokąt,
dziś niewołana z Pythona) **zostaje nietknięta** i nie ma nic wspólnego z komendą
`highlight` — tę obsługuje `encircle`. Zbieżność nazw jest myląca, więc
`cursor.js` dostaje jednozdaniowy komentarz przy obu funkcjach. Usunięcie
martwego `highlight()` to osobna decyzja, poza zakresem tej zmiany.

### Dispatch

`render._render_step` dostaje **jawną** gałąź `cached.action == "highlight"`.
Łańcuch `elif` w tej funkcji nie ma `else`, więc pominięcie gałęzi dałoby krok
bez animacji i bez błędu — cichą awarię tej samej rodziny co niedawno naprawiony
`select` w `guide`. Test 4 (niżej) przypina obie strony kontraktu.

## Przewodnik PDF (`guide`)

- `prolog.ACTION_KINDS` zyskuje `"highlight"` — `classify()` zwraca `"action"`,
  a `scan_for_blockers()` nie odrzuca scenariusza jako nieobsługiwanego. Bez tej
  zmiany preflight z PR #41 zatrzyma cały scenariusz (i dobrze — cicha degradacja
  do strony tekstowej była właśnie tym błędem, który ten preflight naprawiał).
- `capture.py`: gałąź `act == "highlight"` robi `point(…, ripple=False)`, zrzut
  ekranu i **nie** wykonuje żadnej akcji na elemencie (żadnego `click()`,
  `hover()`, `fill()`, `select_option()`). Tu też woła `Highlight.resolved(
  scenario.config.highlight)` i przekazuje `padding` oraz `color` do adnotacji —
  `guide` ma własną nakładkę (`guide/guide.py:67`), ale **nie** animuje kroku:
  ślad jest statyczny, rysowany dopiero w SVG strony PDF.
- `annotate.annotations_for()` zyskuje argumenty `padding: float = 0.0`
  i `color: str | None = None`; dla akcji `highlight` zwraca
  `Annotation(kind="highlight", cx, cy, rx, ry, color=…)` policzone wspólnym
  modułem geometrii (z `fit_to_bounds` na rozmiar zrzutu). Zwyczajowa strzałka od
  poprzedniej pozycji kursora pojawia się jak dla każdej innej akcji z celem.
- `guide/model.py`: `Annotation.kind` zyskuje `"highlight"`, a klasa — pola
  `rx`, `ry` i `color`. Bez pola `color` layout nie ma **skąd** wziąć koloru:
  `_svg()` dostaje wyłącznie listę adnotacji i rozmiar (`layout.py:39`).
- `guide/layout.py`: nowa gałąź rysuje
  `<ellipse class="highlight" cx cy rx ry stroke="…"/>`. Kolor idzie do atrybutu
  `stroke` konkretnego elementu (bo jest per krok), przepuszczony przez
  `html.escape`; klasa CSS niesie tylko grubość linii i `fill: none`.

## Testy (TDD — najpierw czerwone)

Jednostkowe:

1. `Step` — skrót `highlight: "tekst"` normalizuje się do `Highlight(what=…)`;
   pełna postać przyjmuje wszystkie pola; nieznane pole to błąd; `what: "  "` to
   błąd **walidacji modelu** (nie dopiero przy renderze); `loops: 0`, `loops: 6`,
   `padding: -1`, `hold: -1` to błędy.
2. `Step.command_kind() == "highlight"`, `requires_target()` zwraca `True`,
   `optional: true` przechodzi walidację.
3. `Highlight.resolved()` — pola `None` biorą wartość z `HighlightConfig`, pola
   ustawione wygrywają z configiem.
4. Geometria: elipsa zawiera cały prostokąt; margines powiększa obie osie;
   kwadrat daje okrąg (`rx == ry`); `fit_to_bounds` mieści elipsę w kadrze
   (promienie przycięte, środek przesunięty), a element większy od kadru nie
   wywraca funkcji; obwód zgadza się z przybliżeniem Ramanujana dla okręgu
   (`2πr`).
5. `action_for("highlight", …) == "highlight"`, mapa `expected_action`
   w `render.py` zawiera nową parę (obie strony kontraktu w jednym teście),
   a `action_for("teach", "highlight")` **podnosi błąd**.
6. `REASONER_ACTIONS` nie zawiera `"highlight"`, a schemat odpowiedzi budowany
   w `reasoner.py` nie wymienia go w żadnym `enum` — test przypinający, bo to
   właśnie ta pochodna po cichu rozszerzyłaby słownik modelu.
7. Złoty `GOLDEN_MINIMAL_HASH` przechodzi bez zmiany wartości po dodaniu
   `HighlightConfig` do `Config`.
8. `prolog.classify()` zwraca `"action"`, `scan_for_blockers()` nie podnosi
   `GuideError` dla kroku `highlight`.
9. `annotations_for("highlight", …)` zwraca strzałkę i adnotację elipsy
   z właściwym kolorem; przy `box=None` (a więc i `center=None`) nie zwraca
   **żadnej** adnotacji — strzałka też wymaga środka.
10. `layout._svg()` na adnotacji `highlight` zawiera `<ellipse` z właściwym
    `stroke`; kolor z niebezpiecznymi znakami jest escape'owany.
11. `Overlay.encircle()` woła `page.evaluate` z oczekiwanym ładunkiem i pozostawia
    `Overlay.pos` w punkcie wejścia (test z podwójnym `Page`).
12. `cursor.js` — po wstrzyknięciu skryptu `encircle` jest funkcją; guard
    „poprzednia wersja" wymienia `encircle` (rozszerzenie
    `tests/unit/overlay/test_overlay.py`).

Integracyjne:

13. Mini-scenariusz z krokiem `highlight` przechodzi `compile` → `render` →
    `guide` na lokalnej stronie testowej; `*.compiled.yaml` ma akcję `highlight`,
    a HTML przewodnika zawiera `<ellipse`.
14. Krok `highlight` na nieobecnym elemencie z `optional: true` jest pomijany
    zamiast wywracać przebieg.

## Dokumentacja

- `docs/pl/scenario-reference.md` i `docs/en/scenario-reference.md` — opis
  komendy, obie postacie składni, tabela pól, wartości domyślne.
- `docs/pl/pdf-guide.md` i `docs/en/pdf-guide.md` — jak `highlight` wygląda na
  stronie przewodnika.
- Przykładowy scenariusz w `examples/` zyskuje krok `highlight`.

Obie wersje językowe muszą opisywać ten sam zestaw pól i te same wartości
domyślne — rozjazd PL/EN to znany dług, którego tu nie dokładamy.

## Świadomie poza zakresem

- Kształt prostokątny w PDF (`shape: rect`). Zawsze elipsa; gdyby okazała się zła
  dla szerokich tabel, dodanie `shape` będzie zmianą wstecznie zgodną.
- Wskazywanie obszaru współrzędnymi (`{x, y, w, h}`) zamiast opisem semantycznym.
- Zakreślanie kilku elementów jednym krokiem.
- Najechanie na element przy okazji zakreślania (tooltip) — komenda z założenia
  nie dotyka strony.
- Usunięcie martwej funkcji `highlight()` z `cursor.js`.
