# `guidebot guide --headed` — projekt

Data: 2026-07-21
Gałąź: `feat/guide-headed`
Status: zatwierdzony projekt (spec)

## Cel

Dać `guidebot guide` te same możliwości diagnostyczne, które mają `compile`,
`render` i `render-set`: pokazanie okna przeglądarki podczas przechwytywania
(`--headed`) oraz zatrzymanie się na błędzie z otwartym oknem
(`--pause-on-error`).

Dziś `guide` jest jedyną komendą sterującą przeglądarką, która nie ma żadnej z
tych flag — okno jest zawsze niewidoczne, a jedyną diagnostyką pozostaje
`--verbose` oraz katalog `<nazwa>_shots/` obok PDF-a.

## Przesłanka obalona: `page.pdf()` NIE wymaga headless

Poprzedni projekt (`2026-07-20-pdf-step-guide-design.md`, decyzja **S4**)
zakładał, że `page.pdf()` rzuca wyjątek w przeglądarce headed, i wyprowadzał z
tego wniosek, że faza druku musi zawsze działać headless. To założenie
powtórzone jest w repozytorium w trzech miejscach:

- `cli.py:362` — komentarz `# page.pdf() needs headless`,
- `guide/pdf.py:12` — docstring „Browser MUST be headless (page.pdf throws
  otherwise)”,
- `guide/guide.py:34` — docstring „this phase is always headless”.

To jedno źródło przepisane trzy razy, nie trzy niezależne potwierdzenia.
Zweryfikowano je empirycznie 2026-07-21:

| Sprawdzenie | Wynik |
| --- | --- |
| `page.pdf()` na `headless=False`, `set_content` | OK, poprawny PDF (`%PDF-1.4`) |
| `guide.pdf.html_to_pdf()` na `headless=False` (`file://`, `goto(wait_until="load")`, `landscape=True`, `print_background=True`) | OK, 13 810 bajtów |

Środowisko: Playwright 1.61.0, Chromium 149.0.7827.55, macOS (darwin 25.5.0).

Reguła pochodzi z czasów, gdy Chromium miał osobną implementację trybu
headless. „New headless” ujednolicił ścieżki kodu i `Page.printToPDF` jest
dostępne w obu trybach.

**Konsekwencja: decyzja S4 zostaje wycofana, a nie zrealizowana.** Nie jest
potrzebna druga instancja przeglądarki ani rozdzielenie fazy przechwytywania
od fazy druku. Wystarczy jedna przeglądarka uruchomiona z `headless=not
headed`, dokładnie jak w pozostałych komendach.

## Zakres

W zakresie:

- Flagi `--headed` i `--pause-on-error` w `guidebot guide`.
- Zatrzymanie na błędzie w pętli przechwytywania, z redakcją sekretów w
  komunikacie.
- Usunięcie z repozytorium nieprawdziwej reguły „page.pdf wymaga headless”
  (trzy miejsca wymienione wyżej) i odnotowanie wycofania S4 w starym specu.

Poza zakresem (świadomie):

- **Ręczna interwencja w trakcie przebiegu** (domykanie MFA/captcha w oknie,
  jak w `setup --headed`). `guide` odtwarza gotowy sidecar; interwencja
  rozjechałaby stan względem tego, co zamrożono przy `compile`.
- **Redakcja sekretów przy re-raise w `capture.py:155`.** `PlaywrightError`
  jest tam dziś przekazywany dalej bez redakcji, więc nieredagowany komunikat
  może dotrzeć do użytkownika niezależnie od nowych flag. To istniejąca
  usterka, nie regresja tej zmiany — osobne zadanie.
- **Podniesienie dolnej granicy `playwright>=1.47`** w `pyproject.toml`.
  Bardzo stary Chromium mógłby jeszcze podlegać oryginalnemu ograniczeniu, ale
  `--headed` jest ścieżką debugową: awaria byłaby głośna i natychmiastowa, a
  podniesienie floora uderzyłoby we wszystkich użytkowników dla wygody jednej
  flagi.

## Zmiany

### 1. CLI (`guidebot_recorder/cli.py`)

Do `guide_cmd` dochodzą dwie flagi, dosłownie jak w `render_cmd` (cli.py:204-207),
żeby powierzchnia komend pozostała spójna:

```python
headed: bool = typer.Option(False, "--headed", help="Pokaż okno przeglądarki"),
pause_on_error: bool = typer.Option(
    False, "--pause-on-error", help="Przy błędzie zatrzymaj i zostaw okno otwarte (headed)"
),
```

Uruchomienie przeglądarki zmienia się z `launch(headless=True)` na
`launch(headless=not headed)`. Komentarz `# page.pdf() needs headless`
**zostaje usunięty jako nieprawdziwy** (nie przeniesiony gdzie indziej).
`pause_on_error` jest przekazywany do `run_guide`.

### 2. `guidebot_recorder/guide/pdf.py`

Docstring `html_to_pdf` przestaje twierdzić, że przeglądarka musi być
headless. Zostaje opis tego, co funkcja robi.

### 3. `guidebot_recorder/guide/guide.py`

- Sygnatura: nowy parametr `pause_on_error: bool = False` (keyword-only,
  spójnie z `timeout`/`verbose`).
- Docstring przestaje twierdzić, że „this phase is always headless”.
- Liczone są wartości wrażliwe — tak jak w `compile.py:264`:

  ```python
  sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
  ```

  i przekazywane do `capture_pages`. Używane **wyłącznie** do redakcji
  komunikatu pauzy.

### 4. `guidebot_recorder/guide/capture.py`

`capture_pages` przyjmuje `pause_on_error: bool = False` oraz
`sensitive_values: Iterable[str] = ()`. Ciało pętli po krokach zostaje
opakowane w `try/except`, wzorowane na `render.py:2438-2452`:

```python
except Exception as exc:
    if pause_on_error:
        await pause_for_inspection(page, "guide", index, kind, exc, sensitive_values)
    raise
```

Trzy różnice wobec renderera, wynikające z tego, że `guide` jest prostszy:

- pauzujemy na `page`, bez `_active_page(page, popup)` — `guide` odrzuca
  popupy twardo już w prologu (decyzja B6 starego specu), więc strona jest
  zawsze jedna;
- wyjątek leci dalej **nietknięty** (`raise`, nie `raise GuideError(...) from
  None`) — `GuideError` jest już obsłużony w CLI kodem wyjścia 2, a
  opakowywanie zepsułoby te komunikaty;
- `phase="guide"` w komunikacie pauzy.

Ograniczenie znane i akceptowane: `except Exception` nie łapie
`asyncio.CancelledError` (dziedziczy z `BaseException`), więc Ctrl-C nie
uruchamia pauzy. To pożądane — pauza przy przerwaniu przez użytkownika byłaby
irytująca.

### 5. Dokumentacja

`docs/pl/pdf-guide.md` i `docs/en/pdf-guide.md` — opis obu flag. W starym
specu `2026-07-20-pdf-step-guide-design.md` dopisek przy S4, że przesłanka
została obalona empirycznie (z wersjami) i decyzja jest wycofana.

## Testy

Test integracyjny w trybie headed jest niemożliwy w CI (brak wyświetlacza),
więc weryfikacja idzie przez testy jednostkowe:

- `tests/unit/guide/test_capture.py` — monkeypatch `pause_for_inspection`;
  krok rzuca wyjątek → helper wywołany przy `pause_on_error=True`, niewywołany
  przy `False`, a wyjątek w obu przypadkach propaguje dalej.
- `tests/unit/guide/test_capture.py` — komunikat pauzy dostaje przekazane
  `sensitive_values` (asercja na argumencie, nie na treści wydruku).
- `tests/integration/test_guide.py` — bez zmian, musi pozostać zielony; to
  dowód, że domyślna ścieżka headless nie ucierpiała.

Weryfikacja ręczna (poza CI): `uv run guidebot guide <scenariusz> --out
out/g.pdf --headed` pokazuje okno i produkuje poprawny PDF — czyli obalona
przesłanka jest potwierdzona także end-to-end.

## Ryzyka

| Ryzyko | Ocena |
| --- | --- |
| Stary Chromium (`playwright>=1.47`) nie umie drukować headed | Akceptowane; ścieżka debugowa, awaria głośna. Świadomie nie podnosimy floora. |
| `page.pause()` wymaga Playwright Inspectora | Zachowanie identyczne jak w `render --pause-on-error`; helper już to obsługuje i nie maskuje błędu kroku. |
| Zrzuty w `--headed` różnią się od headless (skalowanie HiDPI) | Bez wpływu na produkt: PDF powstaje z tych samych zrzutów, a viewport jest ustawiany jawnie z konfiguracji. |
