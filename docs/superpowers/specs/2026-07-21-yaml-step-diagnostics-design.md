# Diagnostyka kroków: `plik:linia` + fragment YAML (styl Ansible)

Data: 2026-07-21
Status: zatwierdzony projekt, po adwersaryjnym przeglądzie, do wdrożenia

## Problem

Każdy komunikat pipeline'u identyfikuje krok gołym numerem:

```
⚠ krok 13: element opcjonalny 'Zapisz zmiany' nie pojawił się — zapisano wpis oczekujący
krok 16: niezgodna tożsamość — uruchom `compile --force`
```

Żeby dowiedzieć się, o który krok chodzi, autor scenariusza musi ręcznie liczyć
pozycje w YAML-u. Liczenie i tak nie działa, z dwóch niezależnych powodów:

1. **Numeracja jest 0-based** w `compile.py` i `render.py` (`enumerate(flat)` bez
   `+1`), ale 1-based w `recorder/_debug.py:80` (`index + 1`). Ten sam krok ma
   dwa numery zależnie od tego, kto krzyczy.
2. **Indeks jest płaski, nie pozycyjny.** `Scenario.flat_steps()`
   (`models/scenario.py:283`) rozwija każdy blok `when:` w *syntetyczny krok
   bramkujący* + jego dzieci. Bramka nie istnieje w YAML-u jako osobna pozycja
   listy `steps:`, więc od pierwszego bloku `when:` numeracja rozjeżdża się
   z tym, co widać w pliku. `examples/onet-login.scenario.yaml` ma **7 pozycji**
   w `steps:` (w tym jeden blok `when:` z jednym dzieckiem) i **8 kroków
   wykonania** — zmierzone, nie oszacowane.

## Cel

Komunikat ma sam pokazywać, o który krok chodzi: ścieżkę, numer linii i dosłowny
fragment YAML — jak robi to Ansible przy błędzie w playbooku.

## Format wyjścia

### Komunikaty runtime (compile / render / guide / pauza `--pause-on-error`)

Prawdziwe numery dla `examples/onet-login.scenario.yaml` (bramka to 3. krok
z 8; jej dziecko — 4.):

```
⚠ krok 3/8 — examples/onet-login.scenario.yaml:37 (bramka `when:`)
     37 |   - when: "the cookie consent banner"
     38 |     state: visible
     39 |     timeout: 20
   element bramkujący nie pojawił się — zapisano wpis oczekujący (pending);
   render rozwiąże go na miejscu

⚠ krok 4/8 — examples/onet-login.scenario.yaml:41 (w bramce z linii 37)
     41 |       - teach: "First we accept the cookie consent by clicking..."
   element opcjonalny nie pojawił się
```

### Błędy walidacji (przy ładowaniu scenariusza)

Tu winna jest konkretna linia, nie cały krok, więc dochodzi karetka:

```
BŁĄD walidacji — examples/bpp.scenario.yaml:23 (krok 5/12)
     23 |   - click: "Zapisz"
          ^ tutaj
     24 |     navigate: "https://example.test"
   krok ma 2 komend (['navigate', 'click']); dozwolona dokładnie jedna
```

Karetka stoi **bezpośrednio pod linią, do której się odnosi** (jak w Ansible),
a nie na końcu snippetu — inaczej przy kilkulinijkowym kroku nie wiadomo, którą
linię wskazuje.

### Reguły formatu

- Nagłówek runtime: `{prefix}krok {n}/{total} — {ścieżka}:{linia}{sufiks}`,
  gdzie `n` jest **1-based**, `total` = `len(flat_steps())`.
  - `prefix` — `"⚠ "` dla ostrzeżeń, `""` dla błędów (treść wyjątku).
  - `sufiks` — `" (bramka \`when:\`)"` dla kroku bramkującego,
    `" (w bramce z linii {gate_line})"` dla dziecka bloku `when:`, `""` dla
    kroku top-level.
- Nagłówek walidacji: `BŁĄD walidacji — {ścieżka}:{linia} (krok {n}/{total})`.
  Odwrotna kolejność niż w runtime jest zamierzona: przy błędzie walidacji
  pierwszorzędna jest linia, a numer kroku to kontekst.
  - Gdy linii nie da się ustalić: `BŁĄD walidacji — {ścieżka}` bez snippetu.
  - Gdy linia jest znana, ale **nie należy do żadnego kroku** (błąd w `config:`,
    w luce między krokami): `BŁĄD walidacji — {ścieżka}:{linia}`, bez członu
    `(krok n/total)`, a snippet to **sama ta linia**.
- `{ścieżka}` to **`path` dokładnie w formie podanej w wywołaniu CLI**
  (`ScenarioSource.path`), bez normalizacji do absolutnej ani skracania do
  nazwy pliku — tak, żeby dało się ją skopiować z powrotem do terminala lub
  edytora.
- Snippet: `f"{numer_linii:>7} | {treść_linii}"` — numer dosunięty do prawej
  w polu szerokości 7, dosłowna treść linii ze źródła (bez `rstrip`, bez
  zamiany tabów).
- Snippet dłuższy niż **8 linii** ucinany: pierwsze 8 linii + wiersz
  `        … (jeszcze {n} linii)`. **Ucięcie robi wyłącznie `render_banner`** —
  patrz „Właściciel ucięcia" niżej.
- Wiersz karetki: `" " * 10 + "^ tutaj"` — kolumna 11 to pierwszy znak treści
  linii w snippecie (7 na numer + 3 na `" | "`).
- Treść komunikatu wcięta trzema spacjami; wielolinijkowe komunikaty zachowują
  wcięcie w każdej linii.
- Brak lokalizacji (patrz „Degradacja") → nagłówek bez `— plik:linia`, bez
  snippetu, sama treść. Nigdy wyjątek z wyjątku.

## Bezpieczeństwo

**Snippet pochodzi z surowego tekstu pliku, sprzed podstawienia `${ENV}`.**
W snippecie widać `${BPP_PASSWORD}`, nigdy hasło. Wymaga to świadomej decyzji
implementacyjnej: `ScenarioSource` budujemy z tego samego tekstu, który trafia
do `_parse_source`, a **nie** z wyniku `substitute_scenario_values`.

**To nie wystarcza.** Treść komunikatu sklejana obok snippetu bywa już
podstawiona i nieredagowana — `_warn_absent` (`compile.py:494`) drukuje
`_instruction(step)!r` po substytucji, a redakcja w `compile.py` działa tylko
w liniach 337/442. Sekret wyciekłby wierszem niżej pod „bezpiecznym" snippetem.
Dlatego:

- `step_banner` przyjmuje `sensitive: Iterable[str] = ()` i przepuszcza
  **cały złożony banner** przez `redact_text` (`recorder/_debug.py`).
- Wszystkie trzy pipeline'y mają wartości pod ręką i tylko je przekazują:
  `compile.py:264`, `render.py:1874`, `guide/capture.py:60` (parametr).

**Granice gwarancji, zapisane wprost:** chroni to wyłącznie sekrety wstrzykiwane
przez `${ENV}` — bo tylko takie zna `scenario_sensitive_values`. Sekret wpisany
w YAML dosłownie trafi do snippetu verbatim. To nie regresja (dosłowny sekret
i tak leży w repozytorium), ale nie wolno tego reklamować jako ochrony.

Testy regresyjne, oba obowiązkowe:

1. ścieżka walidacji — scenariusz z `${SECRET}` w `enterText.text`, błąd na tym
   kroku, `env={"SECRET": "hunter2"}`, asercja `"hunter2" not in str(exc)`;
2. ścieżka runtime — `_warn_absent` na kroku z `${SECRET}`, asercja
   `"hunter2" not in captured.out`.

## Architektura

### Wybrany wariant: `ScenarioSource` doczepiony do `Scenario`

`scenario/loader.py` obok istniejącego parse'a `YAML(typ="safe")` robi drugi,
**round-trip** (`YAML()`), z którego czyta pozycje węzłów (`.lc`). Wynik —
`ScenarioSource` — dołącza do zbudowanego `Scenario` jako pydantic
`PrivateAttr`, a `flat_steps()` stempluje nim `FlatStep.location`.

Zweryfikowano empirycznie (ruamel 0.19.1, pydantic 2.13.4):

- `CommentedSeq.lc.item(i)` i `CommentedMap.lc.key(k)` zwracają `(linia, kolumna)`
  **0-based**; działają na sekwencji zagnieżdżonej w mapie i dla ostatniego
  elementu. Dla `examples/onet-login.scenario.yaml`: `lc.item(2)` → linia 37,
  `blok.lc.key("steps")` → 40, więc span bramki `[37, 39]`.
- `PrivateAttr` przypisany po `model_validate` działa i przeżywa `model_copy()`;
  `Scenario` nie jest nigdzie w kodzie rekonstruowany (compile/render/guide
  każdorazowo wołają `load_scenario`; `cli.py:262` mutuje `cfg` w miejscu).

Dzięki temu **pętle główne pipeline'ów nie zmieniają sygnatur** — wszystkie trzy
mają `FlatStep` w zasięgu (`compile.py:306`, `render.py:2155`,
`guide/capture.py:68`; preflight `render.py:1913` też, przez `zip(flat, ...)`).
Sygnatury zmieniają natomiast **helpery emitujące komunikaty** — patrz sekcja
„Migracja miejsc wywołań".

### Warianty odrzucone

- **Globalny rejestr `path → SourceMap`.** Zero zmian w modelach, ale głębokie
  funkcje nie mają `path` w zasięgu — trzeba by przewlec go przez kilkanaście
  sygnatur.
- **Cały loader na round-trip, bez `_to_plain`.** Pydantic dostawałby
  `CommentedMap`, które same niosą `.lc`. Odrzucone: `_to_plain`
  (`loader.py:70`) istnieje po to, by ruamelowe skalary nie przeciekały do
  modeli i dalej do `json.dumps` w fingerprintach — ryzyko cichej zmiany hashy
  i unieważnienia całego cache'u kompilacji.

## Komponenty

### `guidebot_recorder/scenario/source.py` (nowy)

```python
@dataclass(frozen=True)
class StepLocation:
    """Gdzie krok płaski mieszka w źródłowym YAML-u (linie 1-based, obustronnie domknięte)."""
    line: int                 # pierwsza linia kroku
    end_line: int             # ostatnia linia kroku
    is_gate: bool             # syntetyczny krok bramkujący bloku `when:`
    gate_line: int | None     # linia `when:` bloku-właściciela; None dla top-level


@dataclass(frozen=True)
class ScenarioSource:
    path: Path
    lines: tuple[str, ...]              # linie pliku, bez znaku końca linii
    steps: tuple[StepLocation, ...]     # indeksowane płaskim indeksem kroku

    def location(self, index: int) -> StepLocation | None
    def snippet(self, loc: StepLocation) -> list[tuple[int, str]]   # PEŁNY span
    def line_snippet(self, line: int) -> list[tuple[int, str]]      # pojedyncza linia
    def node_line(self, loc_path: tuple[str | int, ...]) -> int | None
    def index_at_line(self, line: int) -> int | None


def build_source(path: Path, text: str) -> ScenarioSource
```

#### `build_source` musi być **totalny**

Wołamy go **przed** `model_validate`, więc dostanie także pliki niepoprawne —
i to właśnie dla nich jest najbardziej potrzebny. Naiwna implementacja się
wywala: `YAML().load("config: {}\nsteps: hello\n")["steps"]` to zwykły `str`,
a `.lc` na nim rzuca `AttributeError`. To złamałoby regułę „nigdy wyjątek
z wyjątku".

Wymagania:

- każde zejście w drzewo poprzedzone `isinstance(..., CommentedSeq / CommentedMap)`;
- brak `steps:`, `steps:` skalarem, wpis listy skalarem, blok `when:` bez
  `steps:`, `steps:` bloku niebędące listą → **degradacja do częściowego
  `ScenarioSource`** (tyle spanów, ile dało się policzyć; reszta pominięta),
  nigdy wyjątek;
- błąd parsera YAML (plik składniowo zepsuty) → `ScenarioSource` z pustym
  `steps` i wczytanymi `lines`, bez propagacji wyjątku. Za zgłoszenie błędu
  składni odpowiada istniejąca ścieżka `_parse_source`.

#### Wyznaczanie spanów

`build_source` przechodzi drzewo round-trip **tą samą kolejnością co
`flat_steps()`** (bramka, potem dzieci, potem następny wpis):

- początek wpisu `i` listy `steps:` — `steps.lc.item(i)[0] + 1`;
- koniec = początek następnego rodzeństwa − 1; dla ostatniego wpisu — linia
  przed następnym kluczem top-level po `steps:` (z `root.lc.key(...)`), a gdy
  takiego nie ma — ostatnia linia pliku;
- **przycięcie:** z końca spanu usuwamy linie puste i zawierające wyłącznie
  komentarz;
- **blok `when:`:** span bramki = `[start_bloku, linia_klucza_steps − 1]` po
  przycięciu; gdy wyjdzie pusty (np. `steps:` jest pierwszym kluczem bloku),
  span = `[start, start]`. Klucze bloku zapisane *po* liście `steps:` nie trafią
  do snippetu bramki — świadome uproszczenie na rzecz ciągłego fragmentu;
- dzieci bloku — ta sama reguła następnego rodzeństwa, w obrębie
  `node["steps"]`; koniec ostatniego dziecka = koniec bloku.

**Spany nie pokrywają całego pliku.** Przycinanie zostawia luki: komentarze
i puste linie *między* krokami nie należą do żadnego kroku (`lc.item(i)`
wskazuje linię wpisu, nie komentarza nad nim), podobnie jak cała sekcja
`config:`. Zmierzone spany dla `examples/onet-login.scenario.yaml`:
`(30,30), (33,33), (37,39), (41,41), (43,43), (45,45), (47,47), (49,49)` —
linie 31–32, 34–36, 42, 44, 46, 48 są niczyje. To jest zamierzone i wprost
kontraktowe: **`index_at_line` zwraca `None` dla linii spoza spanów.**

#### `node_line(loc_path)`

Chodzi po drzewie round-trip zgodnie ze ścieżką `loc` pydantica. Element
adresowalny (`node.lc.key(k)` dla mapy, `node.lc.item(i)` dla sekwencji) →
zejdź i zapamiętaj linię. Element **nieadresowalny → pomiń i idź dalej tą samą
ścieżką**, nie przerywaj.

Pominięcie, nie przerwanie, jest istotne: tag wariantu unii to nie `'Step'`,
tylko realnie
`'function-after[_optional_only_where_it_can_be_honoured(), function-after[_exactly_one_command(), Step]]'`,
a błąd dziecka bloku ma `loc == ('steps', 1, 'WhenBlock', 'steps', 0)` —
przerwanie na tagu zmapowałoby go na linię **bloku** zamiast dziecka, choć
reszta ścieżki jest w pełni adresowalna.

Zwraca linię najgłębszego trafionego węzła; `None`, gdy nie skonsumowano nic.

#### Właściciel ucięcia

`snippet(loc)` zwraca **pełny** span. Ucięcie do 8 linii i wiersz
`… (jeszcze n linii)` robi wyłącznie `render_banner`, bo tylko on zna `n`
i tylko on umie wypisać wiersz bez numeru linii (który nie mieści się
w `tuple[int, str]`). `snippet` **nie ma** parametru `max_lines`.

#### Wydajność

`build_source` cache'owany kluczem **`(path, text)`** — nie samym `text`:
identyczna treść pod dwiema ścieżkami musi dać dwa różne `ScenarioSource.path`.

### `guidebot_recorder/diagnostics.py` (nowy)

```python
def render_banner(
    headline: str,
    snippet: list[tuple[int, str]],
    message: str,
    *,
    caret_line: int | None = None,
    max_lines: int = 8,
) -> str

def step_banner(
    *, index: int, total: int, location: StepLocation | None,
    source: ScenarioSource | None, message: str,
    warning: bool = False, sensitive: Iterable[str] = (),
) -> str

def validation_banner(
    *, source: ScenarioSource | None, line: int | None,
    index: int | None, total: int, message: str,
) -> str
```

`render_banner` to jedyne miejsce znające format (numery linii, wcięcia,
karetkę, ucięcie); funkcje wyższego poziomu tylko składają nagłówek i — w
przypadku `step_banner` — redagują wynik.

### `guidebot_recorder/models/scenario.py` (zmiany)

- `FlatStep` zyskuje czwarte pole `location: StepLocation | None = None`.
  **Uwaga:** psuje to istniejący test `tests/unit/models/test_scenario.py:404`
  (`assert tuple(flat) == (flat.step, None, False)` — 4-krotka ≠ 3-krotka).
  Aktualizacja tego testu należy do zadania A. Reszta kodu jest bezpieczna:
  `zip(flat, actions, strict=True)` w `guide/capture.py:68` iteruje po wpisach,
  nie po polach.
- `Scenario` zyskuje `_source: ScenarioSource | None = PrivateAttr(default=None)`,
  property `source` i metodę `attach_source(source)`. `PrivateAttr` nie dotyka
  schematu, więc `extra="forbid"` i serializacja pozostają bez zmian.
- `flat_steps()` stempluje `location` z `self._source` (gdy jest).
- Nowy wyjątek `StepPathError(ValueError)` z polem `path: tuple[int, ...]`
  (ścieżka pozycyjna w `steps:`, np. `(3,)` lub `(3, 1)` dla dziecka bloku).
- `_validate_translations` (linie 320/325/329) przestaje doklejać `krok {label}: `
  do treści i rzuca `StepPathError` z `path`. Etykietę zastępuje nagłówek
  bannera. Istniejące asercje `pytest.raises(ValueError, match="brak tłumaczeń.*en-US")`
  w `tests/unit/scenario/test_loader.py:192+` **muszą dalej przechodzić** —
  `StepPathError` dziedziczy po `ValueError`, a treść zostaje w jednej linii.

### `guidebot_recorder/scenario/loader.py` (zmiany)

```python
def load_scenario(path, env=None) -> Scenario:
    text = Path(path).read_text(encoding="utf-8")
    raw = _parse_source(text)
    # ... istniejąca bramka na sidecar bez zmian ...
    substituted = substitute_scenario_values(raw, env)
    source = build_source(Path(path), text)
    try:
        scenario = Scenario.model_validate(substituted)
    except ValidationError as exc:
        raise ScenarioValidationError(format_validation_error(exc, source, raw)) from None
    scenario.attach_source(source)
    return scenario
```

`ScenarioValidationError(ValueError)` — nowy typ w `loader.py`. Wszystkie
istniejące miejsca łapiące błąd ładowania łapią `Exception` (`cli.py:55`,
`cli.py:65`), więc typ jest zgodny wstecz.

#### `format_validation_error(exc, source, raw)`

**Najpierw filtrowanie wariantów unii — inaczej funkcja produkuje ścianę
sprzecznych bannerów.** Zmierzone: krok `{"click": ..., "navigate": ...}` daje
**5** błędów, z czego 4 to śmieci z wariantu `WhenBlock`
(`('steps',0,'WhenBlock','when') missing`, `… extra_forbidden`, …). Zagnieżdżony
`when` daje 3 błędy, z czego 2 to śmieci z wariantu `Step`. Bez filtra
użytkownik dostałby banner „Extra inputs are not permitted: when" na kroku,
który *ma* być blokiem `when:`.

1. **Grupowanie** błędów po prefiksie `('steps', i)`. Błędy bez tego prefiksu
   (np. `('config', 'viewport')`) przechodzą bez zmian.
2. **Wybór wariantu w grupie:** jeśli surowy wpis `raw["steps"][i]` ma klucz
   `when` → zostają wyłącznie błędy, których tag (`loc[2]`, string) zawiera
   `"WhenBlock"`; w przeciwnym razie — wyłącznie te, których tag **nie** zawiera
   `"WhenBlock"`. Gdy filtr wyczyściłby grupę do zera (nieoczekiwany kształt
   tagu) → zostawiamy grupę w całości; lepiej nadmiar niż zgubiony błąd.
3. **Deduplikacja** par `(linia, treść)`.
4. Dla każdego ocalałego błędu: `line = source.node_line(err["loc"])`;
   jeśli `err["ctx"]["error"]` jest `StepPathError` (pydantic przekazuje
   **oryginalny obiekt wyjątku**, potwierdzone empirycznie — podtyp i atrybut
   `path` przeżywają), jego `path` **nadpisuje** wynik z `loc`. To jedyna droga
   do walidatorów poziomu `Scenario`, które mają `loc == ()`.
5. `index = source.index_at_line(line)` → człon `(krok n/total)`; `None` →
   nagłówek bez tego członu i snippet z `line_snippet(line)`.

Wiele ocalałych błędów → bannery sklejone pustą linią, w kolejności z
`exc.errors()`.

### Migracja miejsc wywołań

29 f-stringów sklejających `krok {…}` przechodzi na `step_banner(...)`, z
zachowaniem dotychczasowej treści komunikatu i dotychczasowego kanału wyjścia
(`tqdm.write` tam, gdzie jest pasek postępu — inaczej pasek rozjedzie się
z tekstem):

| Plik | Linie | Uwagi |
|---|---|---|
| `recorder/compile.py` | 324, 330, 435, 494 | `_warn_absent(index, step, *, gate)` → dochodzą `total`, `source`/`location`, `sensitive` |
| `recorder/render.py` | 1915, 1920, 2145, 2168, 2280, 2332, 2356, 2430, 2738, 2740, 2742, 2745, 2754, 2768, 2827, 2831 | `note_skip` (2143) → dochodzą `total`, `source`, `sensitive`; `_render_step` (def 2612, obejmuje 2738+) → dochodzi `entry`, jedyne wywołanie w pętli głównej (2370) |
| `guide/capture.py` | 147, 154, 156, 161, 168, 174 | `fs` z `zip(flat, actions)` już w zasięgu (68) |
| `recorder/_debug.py` | 80 | `pause_for_inspection` — już 1-based, dochodzi lokalizacja |

`models/scenario.py:261` (`_reject_nested_blocks`, walidator `mode="before"`)
**zostaje bez zmian** — treść komunikatu ta sama, a lokalizację dostaje za darmo
przez mapowanie `loc == ('steps', N, 'WhenBlock')`. Zapisane wprost, żeby
zadanie D nie dotykało pliku edytowanego przez zadanie C.

## Degradacja

`Scenario` zbudowany w kodzie (np. `Scenario(config=..., steps=[...])`
w testach) nie ma `_source`. Wtedy `FlatStep.location is None`, banner traci
`— plik:linia` i snippet, a zostaje `krok 4/8 — treść`. To zamierzone: nie
wolno wymuszać źródła tam, gdzie go nie ma, ani wysypywać się na jego braku.

## Plan testów (TDD — najpierw testy, wszystko bez przeglądarki)

| Plik | Co sprawdza |
|---|---|
| `tests/unit/scenario/test_source.py` | spany kroków top-level; blok `when:` → span bramki bez dzieci; spany dzieci; ostatni krok w pliku; `steps:` niebędące ostatnim kluczem top-level; **luki**: `index_at_line` zwraca `None` dla komentarza/pustej linii między krokami i dla linii w `config:` |
| `tests/unit/scenario/test_source.py` | **totalność**: `steps:` skalarem, wpis listy skalarem, blok `when:` bez `steps:`, brak `steps:`, plik składniowo zepsuty → `ScenarioSource` bez wyjątku |
| `tests/unit/scenario/test_source.py` | `node_line` na **realnych** ścieżkach: `('steps', 0, 'function-after[...]', 'enterText', 'text')`, `('steps', 1, 'WhenBlock', 'steps', 0)` (musi dać linię **dziecka**, nie bloku), `('config', 'viewport')`, `()`, ścieżka nieadresowalna |
| `tests/unit/scenario/test_source.py` (parametryzowany) | **regresja spójności:** dla każdego `examples/*.scenario.yaml` zachodzi `len(build_source(...).steps) == len(load_scenario(...).flat_steps())`, spany rosnące i rozłączne |
| `tests/unit/test_diagnostics.py` | banner z lokalizacją i bez; bramka vs dziecko bramki vs top-level; ucięcie po 8 liniach z wierszem elipsy; karetka pod właściwą linią; wcięcie treści wielolinijkowej; `warning=True` → prefiks `⚠ `; `sensitive` → redakcja całego bannera |
| `tests/unit/scenario/test_loader_validation.py` | **filtr wariantów unii**: dwie komendy w kroku → dokładnie 1 banner (nie 5); zagnieżdżony `when` → dokładnie 1 banner (nie 3); błąd pola / walidator kroku / walidator scenariusza (`StepPathError`); błąd w `config:` → banner bez `(krok n/total)`; fallback bez lokalizacji; **test bezpieczeństwa** ze `${SECRET}` |
| `tests/unit/models/test_scenario.py:404` | `test_flat_step_is_a_named_tuple` — aktualizacja na 4-krotkę (zadanie A) |
| `tests/unit/scenario/test_loader.py:192+` | istniejące asercje `match="brak tłumaczeń.*en-US"` itd. mają przechodzić bez zmian |
| `tests/unit/recorder/test_render_optional.py` | 337, 401, 418 — `match="krok 3"` → 1-based i nowy format |
| `tests/integration/test_optional_branch_compile_render.py` | 230, 270, 271 — `GATE_INDEX` → 1-based i nowy format |
| `tests/unit/recorder/test_compile.py` | **redakcja runtime**: `_warn_absent` na kroku z `${SECRET}` → `"hunter2" not in captured.out` |
| `tests/integration/` (nowy) | `guidebot validate` na zepsutym scenariuszu: kod wyjścia 1, banner na stderr, w nim `plik:linia` i fragment YAML |

## Zmiany zastanego zachowania (świadome)

1. **Komunikaty stają się wielolinijkowe.** CLI drukuje je przez
   `typer.echo(f"BŁĄD: {exc}")` — działa, ale kto parsował output skryptem,
   dostanie inny kształt.
2. **Numeracja przechodzi na 1-based** w `compile` i `render` (dotąd 0-based),
   ujednolicona z `_debug.py`. Numer zyskuje mianownik (`4/8`).
3. **`_validate_translations` traci prefiks `krok {label}: `** z treści — numer
   kroku przenosi się do nagłówka bannera.
4. **Błąd walidacji unii pokazuje jeden banner zamiast pięciu** — filtr wariantu
   celowo ukrywa błędy z niewybranego wariantu unii.

## Dekompozycja pracy (dwie fale, subagenci równolegli)

**Fala 1** — dwa niezależne zadania, rozłączne pliki:

- **A: źródło i model.** `scenario/source.py` (+ testy), `FlatStep.location`,
  `Scenario._source`/`attach_source`, stemplowanie w `flat_steps()`,
  `StepPathError`, dopięcie `build_source` w `load_scenario`
  (bez mapowania błędów walidacji), aktualizacja
  `tests/unit/models/test_scenario.py:404`.
- **B: formatter.** `diagnostics.py` (+ testy). Pisany przeciwko API
  `StepLocation`/`ScenarioSource` zamrożonemu w tym dokumencie; nie czeka na A.

**Fala 2** — po scaleniu fali 1, dwa zadania na rozłącznych plikach:

- **C: mapowanie błędów walidacji.** `format_validation_error` (z filtrem
  wariantów unii), `ScenarioValidationError` w `loader.py`; przejście
  `_validate_translations` na `StepPathError`; testy `test_loader_validation.py`.
- **D: migracja 29 miejsc wywołań** w `compile.py` / `render.py` /
  `guide/capture.py` / `_debug.py` + aktualizacja istniejących asercji + test
  redakcji runtime + test integracyjny `validate`. **Nie dotyka
  `models/scenario.py`.**

Po obu falach: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`.

## Poza zakresem (YAGNI)

- Kolorowanie składni snippetu.
- Limit liczby bannerów przy wielu błędach (filtr wariantów wystarcza).
- Lokalizacja błędów w `*.compiled.yaml` (sidecar jest generowany, nie pisany
  ręcznie) i w manifeście `*.render-set.yaml`.
- Zmiana języka komunikatów.
