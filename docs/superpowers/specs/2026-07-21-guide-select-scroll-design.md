# `guidebot guide`: obsługa `select` i `scroll` oraz prawdziwy powód odrzucenia celu

Data: 2026-07-21
Gałąź: `fix/guide-select-scroll`
Status: zatwierdzony projekt, do wdrożenia

## Objaw

Scenariusz zawierający komendy `select:` i `scroll:` — kompilujący się poprawnie
i renderujący się poprawnie do MP4 przez `render` — kończy się w `guide` dwoma
fałszywymi komunikatami:

```
$ guidebot guide scenarios/02a-wyszukiwanie-publiczne.scenario.yaml -o test.pdf --verbose
pomijam krok 13: oczekiwanie nierozwiązane — uruchom `compile`
BŁĄD: krok 16: niezgodna tożsamość — uruchom `compile --force`
```

Sidecar `*.compiled.yaml` jest świeży (`compile --force`) i ma poprawne wpisy dla
obu kroków. Obie diagnozy są nieprawdziwe, a druga wysyła użytkownika na
kilkanaście minut bezużytecznej rekompilacji.

## Przyczyna źródłowa

`guide` ma **własną** tablicę dyspozycji, niezależną od `render`:
`guide/prolog.py::classify()` mapuje rodzaj kroku na rodzaj strony PDF, a
`guide/capture.py::capture_pages()` rozgałęzia się na `CachedAction.action`.
Komendy `select` (PR #28) i `scroll` (PR #30) zostały dodane do
`render`/`compile`, ale `guide` (PR #32) nigdy ich nie objął — w całym katalogu
`guidebot_recorder/guide/` nie występuje ani słowo `select`, ani `scroll`.

`classify()` kończy się fallbackiem `return "text" if step.say else "wait"`, więc
nieznane komendy cicho wpadają w kategorię „tekst" albo „oczekiwanie":

| krok | scenariusz | `classify()` | skutek |
|---|---|---|---|
| 13 | `- scroll: top` (bez `say`) | `"wait"` | brak `CachedAction/waitFor` → mylące „oczekiwanie nierozwiązane"; **scroll się nie wykonuje** |
| 11 | `- scroll: down` + `say` | `"text"` | strona tekstowa; **scroll się nie wykonuje** |
| 15 | `select: {from: …, option: "Zakres lat"}` + `say` | `"text"` | strona tekstowa; **select się nie wykonuje** |
| 16 | `enterText "2022"` → `textbox nth=1` | `"action"` | w `compile` `nth=1` było polem „rok od" (wiersz 2 był już „Zakres lat"); w `guide` wiersz 2 to nadal „Tytuł pracy", więc `nth=1` trafia w inny element → `reuse_is_valid()` = `False` → `GuideError` |

Błąd kroku 16 jest **konsekwencją** niewykonanego selecta w kroku 15, a nie stanu
kompilacji.

Osobny, niezależny defekt: `capture.py:145-147` odwzorowuje **każdą** porażkę
`reuse_is_valid()` na jedno zdanie „niezgodna tożsamość — uruchom `compile --force`".
`resolver/validate.py` odrzuca cel także z powodów `not_found`, `not_unique`,
`not_visible`, `not_enabled`, `incompatible_type`, `not_select`, `not_editable`,
`dom_changed` — dla nich ta rada jest po prostu błędna. Informacja ginie na
granicy typu `bool` i żadna heurystyka po stronie wywołującego jej nie odtworzy.

## Rozwiązanie

### 1. `classify()` zna `select` i `scroll`

```python
PageKind = Literal["gate", "navigate", "slide", "action", "text", "wait", "scroll"]

if kind == "scroll":
    return "scroll"
if kind in ("click", "hover", "enterText", "teach", "select"):
    return "action"
```

`select` staje się pełnoprawną akcją (ma cel, ma `CachedAction`); `scroll`
dostaje własny rodzaj strony, bo nie ma celu i nie podlega walidacji tożsamości.

### 2. `capture_pages()`: gałąź `scroll`

`scroll` **musi** się wykonać: zrzuty są robione z viewportu (`page.screenshot`
bez `full_page`), więc bez przewinięcia kolejne strony PDF pokazują niewłaściwy
fragment strony.

```python
if kind == "scroll":
    await recorder.scroll(step.scroll_config())
    prev_cursor = None
    text = page_text(step)
    if not text:
        continue
    shot, size = await _screenshot(page, shots_dir, index)
    pages.append(GuidePage(kind="step", screenshot=shot, text=text,
                           heading=None, annotations=[], screenshot_size=size))
    continue
```

Decyzje:

- **Strona PDF tylko gdy krok niesie tekst** (`caption` albo `say`/`teach`, czyli
  niepuste `page_text(step)`). Wtedy zrzut jest robiony **po** przewinięciu i
  tekst opisuje to, co właśnie weszło w kadr. Gołe `- scroll: top` przewija i nie
  generuje żadnej strony — inaczej PDF zbierałby puste kadry bez opisu.
- **`prev_cursor = None`.** Strzałka jest rysowana od poprzedniego celu do
  bieżącego we współrzędnych *viewportu*. Po przewinięciu stare współrzędne nie
  wskazują już niczego, więc kursor trzeba wyzerować — dokładnie tak, jak robi to
  gałąź `navigate` (`capture.py:102`).
- Strona ma `kind="step"` i pustą listę adnotacji; nowy wariant w
  `GuidePage.kind` nie jest potrzebny.
- `guide` **instaluje overlay** (`guide/guide.py:63-67`, `guide.py:83`), więc
  `Recorder.scroll` pójdzie ścieżką animowaną (`recorder.py:240-248`: 16 kroków
  × 18 ms + 150 ms ≈ 0,44 s na krok). To akceptowalne — kadr i tak jest robiony
  po zakończeniu animacji, a kod jest wspólny z `render`.

### 3. `capture_pages()`: gałąź `select`

W dispatchu na `action.action`, obok `type` / `hover` / `click`:

```python
elif act == "select":
    if step.select is None:
        raise GuideError(
            f"krok {index}: sidecar mówi `select`, a krok scenariusza nim nie jest "
            "— uruchom `compile --force`"
        )
    await res.locator.select_option(label=step.select.option)
    shot, size = await _screenshot(page, shots_dir, index)  # kadr PO wyborze
```

Decyzje:

- **Etykieta opcji pochodzi z `step.select.option`**, nie z sidecara — dokładnie
  jak `render.py:2832`. Sidecar nie zamraża etykiety opcji (`input_text` jest
  zarezerwowane dla akcji `type`).
- **Zrzut PO wybraniu opcji.** Natywna lista opcji jest rysowana przez OS i żadne
  narzędzie automatyzujące przeglądarkę jej nie zrzuci; zwinięta kontrolka po
  wyborze pokazuje wartość docelową, więc czytelnik widzi, co ma osiągnąć. To ten
  sam układ, co dla `type` (`capture.py:161`).
- **`res.locator.select_option(label=…)`, nie `recorder.select()`.** `guide` trzyma
  wzorzec „point → (akcja) → zrzut" i `res.locator` pochodzi z już wykonanego
  `recorder.point()`; `recorder.select()` wykonałoby drugie `_point_and_prepare`.
  Co ważniejsze: `guide` **ma** zainstalowany overlay (`guide/guide.py:63-67, 83`),
  więc `Recorder.select` poszedłby ścieżką `_step_option_visibly`
  (`recorder.py:179-212`) — stepowanie strzałkami po 140 ms na opcję z dźwiękiem
  klawisza. Dla nieruchomego kadru to czysty koszt bez zysku; `select_option`
  ustawia wartość natychmiast i deterministycznie.
- **Niespójność sidecar↔scenariusz jest błędem, nie cichym pominięciem.** Pydantic
  wymusza `option` na modelu `Select`, więc jedyny realny przypadek to wpis
  `action: select` w sidecarze przy kroku, który selectem nie jest — czyli
  nieaktualny sidecar. Stąd rada `compile --force` w komunikacie.

### 4. Adnotacja `selected`

`Annotation.kind` (`guide/model.py:16`) dostaje wariant `"selected"`; `annotate.annotations_for()`
zwraca go dla `action == "select"`, gdy znany jest `box`;
`layout.py:47` renderuje `("typed", "hover", "selected")` tym samym czerwonym
prostokątem. Wizualnie bez zmian — chodzi o to, żeby model nie twierdził, że w
`<select>` „wpisano tekst".

### 5. `reuse_failure()` — powód zamiast `bool`

W `resolver/validate.py`:

```python
ReuseReason: TypeAlias = ValidationReason | Literal[
    "identity_mismatch", "identity_missing", "no_wait_state",
    "wait_ambiguous", "sensitive_target",
]

async def reuse_failure(page: Page | Frame, cached: CachedAction) -> ReuseReason | None:
    """Zwraca powód odrzucenia zamrożonej akcji albo None, gdy nadaje się do użycia."""

async def reuse_is_valid(page: Page | Frame, cached: CachedAction) -> bool:
    return await reuse_failure(page, cached) is None
```

**Równoważność jest wymogiem twardym, nie kosmetyką.** `reuse_is_valid` ma trzy
miejsca wywołania: `render.py:2741` (odtwarzanie), `render.py:2276` (probe kroków
opcjonalnych) i `compile.py:649` — to ostatnie decyduje, czy wpis da się reużyć,
czyli czy `compile` odpali LLM-a. Każde odchylenie semantyki uderza w koszt i
determinizm kompilacji.

Pełny kształt funkcji (kolejność, zasięg `try` i ścieżki sukcesu są istotne):

```python
async def reuse_failure(page, cached):
    try:
        if cached.action == "waitFor":
            if cached.state is None:
                return "no_wait_state"
            if cached.state == "hidden":
                # UWAGA: to jest ścieżka wyjścia także dla SUKCESU. Hidden-wait
                # z definicji nie ma tożsamości (models/action.py:87-88), więc
                # NIE WOLNO puścić go dalej do sprawdzeń tożsamości — inaczej
                # każdy poprawny hidden-gate dostanie `identity_missing`,
                # `compile` przestanie go reużywać i re-resolvuje go LLM-em.
                locator = await build_locator(page, cached.target)
                return None if await locator.count() <= 1 else "wait_ambiguous"

        result = await validate_compile_time(page, cached.target, cached.action)
        if isinstance(result, ValidationFail):
            return result.reason
        if cached.identity is None:
            return "identity_missing"
        if (
            cached.action == "type"
            and cached.fingerprint.command_kind == "teach"
            and await is_sensitive_type_target(result.locator)
        ):
            return "sensitive_target"
        current_identity = await capture_identity(result.locator)
    except (PlaywrightError, ValueError):
        return "dom_changed"
    # poza `try` — dokładnie jak dziś (validate.py:235)
    return None if cached.identity.matches(current_identity) else "identity_mismatch"
```

Odwzorowanie obecnych ścieżek `False` jest 1:1:

| ścieżka w `reuse_is_valid` | nowy powód |
|---|---|
| `waitFor` bez `state` | `no_wait_state` |
| `waitFor`/`hidden` z `count() > 1` | `wait_ambiguous` |
| `validate_compile_time()` → `ValidationFail` | `result.reason` (bez zmian) |
| `cached.identity is None` | `identity_missing` |
| wrażliwe pole dla `teach`→`type` | `sensitive_target` |
| `except (PlaywrightError, ValueError)` | `dom_changed` |
| `identity.matches()` = `False` | `identity_mismatch` |

Ścieżki sukcesu (`None`) są dwie i obie muszą przetrwać: `waitFor`/`hidden`
z `count() <= 1` **oraz** zgodna tożsamość na końcu.

`ValidationReason` (słownik używany do re-promptów resolvera) **pozostaje
nietknięty** — nowe powody żyją w osobnym aliasie `ReuseReason`.
Wszystkie trzy call site'y `reuse_is_valid` zachowują dotychczasowe zachowanie.

### 6. `guide` raportuje prawdziwy powód

`capture.py` woła `reuse_failure()` i mapuje powód na polskie zdanie. Radę
`compile --force` dostają **tylko** powody tożsamościowe:

```python
_REUSE_REASON_PL = {
    "not_found": "celu nie ma na stronie",
    "not_unique": "cel pasuje do wielu elementów",
    "not_visible": "cel jest niewidoczny",
    "not_enabled": "cel jest nieaktywny",
    "not_editable": "cel nie przyjmuje tekstu",
    "incompatible_type": "typ elementu nie pasuje do akcji",
    "not_select": "cel nie jest natywnym <select>",
    "unsupported_action": "akcja nieobsługiwana przez walidację",
    "dom_changed": "strona zmieniła się w trakcie sprawdzania",
    "identity_mismatch": "niezgodna tożsamość — uruchom `compile --force`",
    "identity_missing": "wpis bez zamrożonej tożsamości — uruchom `compile --force`",
    "no_wait_state": "wpis oczekiwania bez stanu — uruchom `compile`",
    "wait_ambiguous": "oczekiwanie pasuje do wielu elementów",
    "sensitive_target": "cel wygląda na pole wrażliwe — `teach` go nie wypełni",
}
```

Nieznany powód wpada w `.get(reason, reason)`, czyli w najgorszym razie
użytkownik zobaczy surowy identyfikator, a nie fałszywą radę.

**Semantyka pozostaje bez zmian:** krok obowiązkowy z niepustym powodem nadal
**zawsze** kończy się `GuideError` (jak dziś, `capture.py:146-147`). Zmienia się
wyłącznie treść zdania — nic nie zaczyna być po cichu pomijane.

Wpisy `no_wait_state` i `wait_ambiguous` są w `guide` dziś nieosiągalne (walidacja
odpala się tylko dla `act != "waitFor"`, `capture.py:145`). Zostają w mapie dla
kompletności typu `ReuseReason`, żeby przyszły użytkownik `reuse_failure`
(np. `compile`) nie dostał surowego identyfikatora.

## Testy (TDD — najpierw czerwone)

`tests/unit/guide/*` używają fake'ów bez przeglądarki (`FakeLocator`,
`FakeRecorder`, `FakePage`); `tests/unit/resolver/test_validate.py` uruchamia
prawdziwego headless Chromium mimo katalogu `unit/`.

**`tests/unit/guide/test_prolog.py`**
- `classify()` zwraca `"action"` dla kroku `select`.
- `classify()` zwraca `"scroll"` dla `scroll` — zarówno z `say`, jak i bez.

**`tests/unit/guide/test_capture.py`** (rozszerzyć `FakeLocator` o
`select_option`, `FakeRecorder` o `scroll`, dodać wspólny log zdarzeń)

*Najpierw migracja seamu:* cztery istniejące testy patchują
`capture.reuse_is_valid` (linie 92, 123, 136, 158). Po przejściu `capture.py` na
`reuse_failure` te monkeypatche stają się martwe, a prawdziwe `reuse_failure`
dostanie `FakeRecorder.frame = object()` i wysypie się `AttributeError`
(nieprzechwytywanym — `reuse_failure` łapie tylko `PlaywrightError, ValueError`).
Seam trzeba przenieść na `capture.reuse_failure` zwracające `None` / powód.

- krok `select` woła `select_option(label="Zakres lat")` i **dopiero potem**
  robi zrzut (kolejność weryfikowana wspólnym logiem zdarzeń).
- strona z kroku `select` ma adnotację `kind="selected"`.
- `scroll` bez tekstu: `recorder.scroll` wywołany ze znormalizowanym `Scroll`,
  `pages == []`.
- `scroll` z `say`: `recorder.scroll` wywołany, powstaje strona ze zrzutem i
  tekstem.
- reset kursora — sekwencja **akcja → scroll → akcja**. Bez tego test jest pusty:
  `prev_cursor` nigdy nie zostałby ustawiony i asercja przeszłaby również bez
  poprawki. `FakeRecorder.point` zwraca stałe `center=(5.0, 5.0)`, więc bez resetu
  powstałaby strzałka (5,5)→(5,5) — różnica jest obserwowalna.
- powód `not_found` daje komunikat bez `compile --force`; `identity_mismatch`
  daje komunikat z `compile --force`.

**`tests/unit/resolver/test_validate.py`**
- `reuse_failure()` zwraca `"not_found"`, `"not_visible"` i `"identity_mismatch"`
  dla odpowiednio spreparowanego DOM-u, oraz `None` dla poprawnego celu.
- `reuse_is_valid()` nadal zwraca `bool` (regresja opakowania).

**`tests/integration/test_guide.py`** — dowód end-to-end bez LLM-a: nowy fixture
HTML z `<select>` (zmieniającym treść strony) i zawartością poniżej zagięcia,
scenariusz z `select:` + `scroll:`, kompilacja `MockReasoner`em, `run_guide` →
prawdziwy PDF. Asercje: PDF niepusty, liczba stron zgodna z regułą „scroll bez
tekstu nie daje strony", a select faktycznie zmienił wartość kontrolki.

## Znane ograniczenie (poza zakresem tej zmiany)

`Recorder.point` robi `scrollIntoView({block: 'center'})` przy **każdej** akcji
(`recorder.py:87`). Jeśli cel kolejnego kroku był poza kadrem, strona przewija się
niejawnie i `prev_cursor` z poprzedniego kroku wskazuje już inne miejsce
viewportu — strzałka bywa więc myląca także bez jawnego `scroll:`. Zerowanie
kursora po `scroll` jest spójne z gałęzią `navigate`, ale **nie domyka** tego
tematu. Pełne rozwiązanie (śledzenie przesunięcia scrolla między krokami i
korygowanie współrzędnych albo zerowanie kursora, gdy `point()` przewinął stronę)
jest osobnym zadaniem.

## Poza zakresem

- `render` i `compile` — bez zmian.
- Słownik `ValidationReason` używany do re-promptów resolvera — bez zmian.
- Layout PDF (marginesy, czcionki, kolory) — bez zmian.
- Wsparcie dla popupów, wielojęzyczności i grupowania kroków — nadal poza `guide` v1.

## Dokumentacja

`docs/pl/pdf-guide.md` i `docs/en/pdf-guide.md`, sekcja „Obecne ograniczenia v1" /
„Current v1 limitations": przestać milczeć o `select`/`scroll`. Opisać, że
`select` wykonuje się i daje kadr po wyborze opcji (lista opcji jest rysowana
przez system operacyjny i nie da się jej zrzucić), a `scroll` wykonuje się zawsze
i produkuje stronę tylko wtedy, gdy krok niesie `say`/`caption`.

## Weryfikacja

1. `uv run ruff check .` i `uv run ruff format --check .` (line-length 100).
2. `uv run pytest tests/unit` — zielone, wraz z nowymi przypadkami.
3. `uv run pytest tests/integration/test_guide.py` — nowy przypadek
   `select` + `scroll` produkuje prawdziwy PDF.
4. Ręcznie: `guide` na scenariuszu BPP z `select` + `scroll` przechodzi krok 16.
   **Na tym hoście niewykonalne** — demo BPP na `http://127.0.0.1:8000` nie
   odpowiada, a repo `bpp-guides` nie istnieje. Zastępczo służy punkt 3
   (prawdziwy Chromium, prawdziwa kompilacja, prawdziwy PDF). Braku demo nie
   wolno zamiatać pod dywan — musi trafić do opisu PR.
