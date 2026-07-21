# Plan wdrożenia: `select` i `scroll` w `guidebot guide`

Spec: `docs/superpowers/specs/2026-07-21-guide-select-scroll-design.md`
Gałąź: `fix/guide-select-scroll`

## Global Constraints

Obowiązują KAŻDE zadanie:

- **TDD bezwzględnie.** Najpierw test, który failuje z obecnym kodem (uruchom go
  i pokaż czerwony wynik), dopiero potem implementacja. W raporcie musi znaleźć
  się output czerwonego i zielonego przebiegu.
- **Żadnego cichego łykania wyjątków.** Każdy `except` loguje, re-raise'uje albo
  zwraca sensowny błąd. Nie dodawaj `except Exception`.
- **Jakość:** `uv run ruff check .` i `uv run ruff format --check .` muszą
  przechodzić (line-length 100). `uv run pytest tests/unit` musi być zielone.
- **Język:** kod i docstringi po angielsku (jak reszta repo), komunikaty błędów
  dla użytkownika po polsku (jak reszta repo).
- **Commity:** conventional commits, po polsku, zakres `fix(guide)` /
  `refactor(resolver)` / `test(guide)` / `docs` zależnie od zadania. Stopka:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Nie ruszaj `render.py` ani `compile.py`.** Zmiana ma być dla nich
  bezinwazyjna.
- **Nie dopisuj funkcji spoza speca.** Żadnych flag CLI, żadnych nowych opcji
  konfiguracji, żadnego refaktoru „przy okazji".

---

## Task 1: `reuse_failure()` w `resolver/validate.py`

**Pliki:** `guidebot_recorder/resolver/validate.py`,
`tests/unit/resolver/test_validate.py`

**Cel:** wydobyć powód odrzucenia zamrożonej akcji, który dziś ginie w `bool`.

**Co zrobić:**

1. Dodaj alias typu obok istniejącego `ValidationReason` (nie modyfikuj samego
   `ValidationReason` — jest używany do re-promptów resolvera):

   ```python
   ReuseReason: TypeAlias = ValidationReason | Literal[
       "identity_mismatch",
       "identity_missing",
       "no_wait_state",
       "wait_ambiguous",
       "sensitive_target",
   ]
   ```

2. Zamień `reuse_is_valid` na cienkie opakowanie nowej funkcji
   `reuse_failure(page: Page | Frame, cached: CachedAction) -> ReuseReason | None`
   (`None` = wpis nadaje się do użycia):

   ```python
   async def reuse_failure(page, cached):
       try:
           if cached.action == "waitFor":
               if cached.state is None:
                   return "no_wait_state"
               if cached.state == "hidden":
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
       return None if cached.identity.matches(current_identity) else "identity_mismatch"


   async def reuse_is_valid(page, cached) -> bool:
       return await reuse_failure(page, cached) is None
   ```

**KRYTYCZNE — dwie ścieżki sukcesu:** gałąź `waitFor`/`hidden` przy
`count() <= 1` zwraca `None` **natychmiast**, nie schodząc do sprawdzeń
tożsamości. Hidden-wait z definicji nie ma zamrożonej tożsamości
(`models/action.py:87-88`), więc puszczenie go dalej dałoby `identity_missing`
dla każdej poprawnej bramki — a `compile.py:649` przestałby je reużywać i
re-resolvował je LLM-em przy każdej kompilacji. Druga ścieżka sukcesu to zgodna
tożsamość na końcu. Finalne `identity.matches()` zostaje **poza** blokiem `try`,
dokładnie jak dziś.

`reuse_is_valid` ma trzy miejsca wywołania (`render.py:2741`, `render.py:2276`,
`compile.py:649`) — żadne nie może zmienić zachowania. Zachowaj docstring
wyjaśniający, że wrapper istnieje dla tych wywołujących.

**Testy** (w `tests/unit/resolver/test_validate.py`; plik ma już fixture `page`
odpalającą prawdziwego headless Chromium — użyj jej, wzoruj się na istniejących
testach `test_reuse_*`):

- `reuse_failure` zwraca `None` dla poprawnego celu ze zgodną tożsamością.
- `reuse_failure` zwraca `"identity_mismatch"`, gdy tożsamość się rozjechała
  (wzoruj się na `test_reuse_is_invalid_when_captured_identity_differs`).
- `reuse_failure` zwraca `"not_found"`, gdy celu nie ma w DOM-ie.
- `reuse_failure` zwraca `"sensitive_target"` dla `teach`→`type` na polu
  `type="password"` (wzoruj się na
  `test_reuse_rejects_teach_type_on_password_field`).
- **Regresja ścieżki hidden:** `reuse_failure` zwraca `None` dla
  `action="waitFor"`, `state="hidden"`, `identity=None`, gdy element nie
  istnieje (`count() == 0`) — to jest właśnie ten przypadek, w którym błędna
  implementacja zwróciłaby `identity_missing`.
- `reuse_is_valid` nadal zwraca `True`/`False` (nie powód) — regresja opakowania.

---

## Task 2: `select` i `scroll` w `guide` + prawdziwy komunikat błędu

**Pliki:** `guidebot_recorder/guide/prolog.py`, `guidebot_recorder/guide/capture.py`,
`guidebot_recorder/guide/annotate.py`, `guidebot_recorder/guide/model.py`,
`guidebot_recorder/guide/layout.py`, `tests/unit/guide/test_prolog.py`,
`tests/unit/guide/test_capture.py`

**Kontekst:** `guide` ma własną tablicę dyspozycji, niezależną od `render`.
Komendy `select` (PR #28) i `scroll` (PR #30) nigdy jej nie objęły, więc wpadają
w fallback `classify()` (`return "text" if step.say else "wait"`) i **nie
wykonują się**. Niewykonany `select` przesuwa DOM tak, że kolejny target `nth=1`
trafia w inny element — i użytkownik dostaje fałszywe „niezgodna tożsamość —
uruchom `compile --force`".

**Co zrobić:**

1. **`prolog.py`** — `PageKind` dostaje wariant `"scroll"`; `classify()`:
   ```python
   if kind == "scroll":
       return "scroll"
   if kind in ("click", "hover", "enterText", "teach", "select"):
       return "action"
   ```

2. **`capture.py`, gałąź `scroll`** (przed dispatchem akcji; `scroll` nie ma celu
   i nie ma `CachedAction` — `action` jest `None`):
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
   `scroll` **musi** się wykonać zawsze — zrzuty są z viewportu
   (`page.screenshot` bez `full_page`), więc bez przewinięcia kolejne strony PDF
   pokazują niewłaściwy fragment. Strona PDF powstaje tylko, gdy krok niesie
   tekst (`page_text(step)` niepuste), bo gołe `scroll: top` dawałoby pusty kadr
   bez opisu. `prev_cursor = None` jest konieczne: strzałka jest rysowana we
   współrzędnych viewportu, a po przewinięciu stare współrzędne nie wskazują już
   niczego — tak samo robi gałąź `navigate` (`capture.py:102`).

3. **`capture.py`, gałąź `select`** w dispatchu na `action.action`, obok
   `type`/`hover`/`click`:
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
   Etykieta opcji pochodzi ze scenariusza (`step.select.option`), nie z sidecara
   — tak samo jak `render.py:2832`. Zrzut jest robiony **po** wyborze: natywna
   lista opcji jest rysowana przez system operacyjny i nie da się jej zrzucić, a
   zwinięta kontrolka po wyborze pokazuje wartość docelową. Używamy
   `res.locator.select_option(...)`, a **nie** `recorder.select(...)`: `guide` ma
   zainstalowany overlay (`guide/guide.py:63-67, 83`), więc `Recorder.select`
   poszedłby ścieżką animowanego stepowania strzałkami po 140 ms na opcję —
   czysty koszt dla nieruchomego kadru — i zrobiłby drugie `_point_and_prepare`.

4. **Adnotacja `selected`:** `Annotation.kind` w `guide/model.py:16` dostaje
   wariant `"selected"`; `annotate.annotations_for()` zwraca
   `Annotation(kind="selected", x=…, y=…, w=…, h=…)` dla `action == "select"`,
   gdy `box is not None` (analogicznie do `typed`); `layout.py:47` renderuje
   `elif a.kind in ("typed", "hover", "selected")` tym samym prostokątem.
   Wizualnie bez zmian — chodzi o to, żeby model nie twierdził, że w `<select>`
   „wpisano tekst".

5. **Prawdziwy powód błędu:** `capture.py` przechodzi z `reuse_is_valid` na
   `reuse_failure` (Task 1 już ją dodał) i mapuje powód na polskie zdanie.
   Semantyka bez zmian: krok obowiązkowy z niepustym powodem **nadal zawsze**
   kończy się `GuideError` — zmienia się wyłącznie treść zdania.
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
   Nieznany powód → `.get(reason, reason)`, żeby w najgorszym razie użytkownik
   zobaczył surowy identyfikator, a nie fałszywą radę. Rada `compile --force`
   pada **tylko** przy powodach tożsamościowych — jest już wpisana w treść tych
   dwóch zdań, nie doklejaj jej osobno.

**Testy:**

`tests/unit/guide/test_prolog.py`:
- `classify()` zwraca `"action"` dla kroku `select`.
- `classify()` zwraca `"scroll"` dla `scroll` — osobno z `say` i bez `say`
  (dziś odpowiednio `"text"` i `"wait"`, więc oba przypadki są czerwone).

`tests/unit/guide/test_capture.py`:
- **Najpierw przenieś seam monkeypatcha.** Cztery istniejące testy patchują
  `capture.reuse_is_valid` (linie 92, 123, 136, 158). Po przejściu na
  `reuse_failure` te monkeypatche staną się martwe, a prawdziwe `reuse_failure`
  dostanie `FakeRecorder.frame = object()` i wysypie się `AttributeError`
  (nieprzechwytywanym — łapane są tylko `PlaywrightError, ValueError`). Zamień je
  na patche `capture.reuse_failure` zwracające `None` (OK) albo powód.
- Rozszerz `FakeLocator` o `select_option(label=...)`, `FakeRecorder` o
  `scroll(spec)`, a `FakePage.screenshot` i akcje o **wspólny log zdarzeń**, żeby
  dało się zweryfikować kolejność.
- krok `select` woła `select_option(label="Zakres lat")` i **dopiero potem** robi
  zrzut (asercja na kolejności w logu zdarzeń).
- strona z kroku `select` ma adnotację `kind="selected"`.
- `scroll` bez `say`: `recorder.scroll` wywołany ze znormalizowanym `Scroll`,
  `pages == []`.
- `scroll` z `say`: `recorder.scroll` wywołany, powstaje jedna strona ze zrzutem
  i tekstem.
- **reset kursora** — scenariusz musi mieć sekwencję **akcja → scroll → akcja**.
  Bez kroku akcji PRZED scrollem `prev_cursor` nigdy nie zostaje ustawiony i
  asercja przechodzi także bez poprawki, czyli test jest pusty.
  `FakeRecorder.point` zwraca stałe `center=(5.0, 5.0)`, więc bez resetu
  powstałaby strzałka (5,5)→(5,5) — różnica jest obserwowalna. Asercja: strona
  akcji po scrollu nie ma adnotacji `kind="arrow"`.
- powód `not_found` daje `GuideError` **bez** frazy `compile --force`; powód
  `identity_mismatch` daje `GuideError` **z** tą frazą.

---

## Task 3: dowód end-to-end i dokumentacja

**Pliki:** `tests/integration/test_guide.py`,
`tests/integration/fixtures/` (nowy plik HTML), `docs/pl/pdf-guide.md`,
`docs/en/pdf-guide.md`

**Co zrobić:**

1. **Test integracyjny** — `tests/integration/test_guide.py` ma już harness, który
   kompiluje scenariusz `MockReasoner`em (bez LLM-a) i puszcza `run_guide` przez
   prawdziwego headless Chromium do prawdziwego PDF-a. Dołóż analogiczny
   przypadek dla `select` + `scroll`:
   - nowy fixture HTML w `tests/integration/fixtures/` z natywnym `<select>`
     (min. 2 opcje) i treścią poniżej zagięcia (viewport w scenariuszu to
     800×600, więc zadbaj o wysoką stronę, np. blok o `height: 1200px`);
   - scenariusz z krokiem `select:` (`from`/`option`) i co najmniej dwoma
     krokami `scroll:` — jednym z `say` i jednym bez, żeby udowodnić regułę
     „scroll bez tekstu nie daje strony";
   - `MockReasoner` musi zwrócić `ReasonerResult("select", …)` dla instrukcji
     selecta — zobacz, jak istniejący mock rozgałęzia się po treści instrukcji;
   - asercje: PDF istnieje i jest niepusty, liczba zwróconych stron zgadza się z
     regułą o `scroll` bez tekstu.

   Ten test jest **jedynym** dowodem end-to-end, jakim dysponujemy: demo BPP na
   `http://127.0.0.1:8000` nie działa na tym hoście, a repo `bpp-guides` nie
   istnieje. Nie osłabiaj asercji, żeby test „przeszedł".

2. **Dokumentacja** — `docs/pl/pdf-guide.md` i `docs/en/pdf-guide.md`, sekcja
   „Obecne ograniczenia v1" / odpowiednik angielski (ok. linie 78-91). Dziś oba
   pliki milczą o `select`/`scroll`, przez co użytkownik nie wie, czego się
   spodziewać. Opisz:
   - `select` wykonuje się i produkuje stronę ze zrzutem **po** wybraniu opcji —
     natywna lista opcji jest rysowana przez system operacyjny i żadne narzędzie
     automatyzujące przeglądarkę jej nie zrzuci, więc przewodnik pokazuje
     zwiniętą kontrolkę z już ustawioną wartością;
   - `scroll` wykonuje się zawsze (zrzuty są z viewportu, więc przewinięcie jest
     konieczne, żeby kolejne strony pokazywały właściwy fragment), ale stronę PDF
     produkuje tylko wtedy, gdy krok niesie `say` albo `caption`.

   Trzymaj styl i strukturę obu plików; treść PL i EN ma być równoważna.
