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

### 3. `capture_pages()`: gałąź `select`

W dispatchu na `action.action`, obok `type` / `hover` / `click`:

```python
elif act == "select":
    option = step.select.option if step.select else None
    if option is None:
        raise GuideError(f"krok {index}: krok select bez `option` — sprawdź scenariusz")
    await res.locator.select_option(label=option)
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
  Przy `overlay=None` (a `guide` overlaya nie ma) `Recorder.select` i tak sprowadza
  się do `select_option(label=…)` (`recorder.py:174-176`), więc zachowanie jest
  identyczne.
- **Brak `option` jest błędem, nie cichym pominięciem** — pydantic wymusza
  `option` na modelu `Select`, więc ta gałąź jest obroną przed niespójnością
  sidecar↔scenariusz, nie normalną ścieżką.

### 4. Adnotacja `selected`

`Annotation.kind` dostaje wariant `"selected"`; `annotate.annotations_for()`
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

`ValidationReason` (słownik używany do re-promptów resolvera) **pozostaje
nietknięty** — nowe powody żyją w osobnym aliasie `ReuseReason`.
`render.py:2742` używa dalej `reuse_is_valid` i nie zmienia zachowania.

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

## Testy (TDD — najpierw czerwone)

`tests/unit/guide/*` używają fake'ów bez przeglądarki (`FakeLocator`,
`FakeRecorder`, `FakePage`); `tests/unit/resolver/test_validate.py` uruchamia
prawdziwego headless Chromium mimo katalogu `unit/`.

**`tests/unit/guide/test_prolog.py`**
- `classify()` zwraca `"action"` dla kroku `select`.
- `classify()` zwraca `"scroll"` dla `scroll` — zarówno z `say`, jak i bez.

**`tests/unit/guide/test_capture.py`** (rozszerzyć `FakeLocator` o
`select_option`, `FakeRecorder` o `scroll`, dodać wspólny log zdarzeń)
- krok `select` woła `select_option(label="Zakres lat")` i **dopiero potem**
  robi zrzut (kolejność weryfikowana logiem zdarzeń).
- strona z kroku `select` ma adnotację `kind="selected"`.
- `scroll` bez tekstu: `recorder.scroll` wywołany, `pages == []`.
- `scroll` z `say`: `recorder.scroll` wywołany, powstaje strona ze zrzutem i
  tekstem, a następna strona akcji **nie** ma strzałki (kursor wyzerowany).
- powód `not_found` daje komunikat bez `compile --force`; `identity_mismatch`
  daje komunikat z `compile --force`.

**`tests/unit/resolver/test_validate.py`**
- `reuse_failure()` zwraca `"not_found"`, `"not_visible"` i `"identity_mismatch"`
  dla odpowiednio spreparowanego DOM-u, oraz `None` dla poprawnego celu.
- `reuse_is_valid()` nadal zwraca `bool` (regresja opakowania).

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
3. Ręcznie: `guide` na scenariuszu z `select` + `scroll` przechodzi krok 16 i
   produkuje PDF (wymaga lokalnego demo BPP na `http://127.0.0.1:8000`; brak demo
   należy zaraportować wprost, nie udawać weryfikacji).
