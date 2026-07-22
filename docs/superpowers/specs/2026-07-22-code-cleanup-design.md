# Sprzątanie kodu: limit 600 linii na plik, cyclomatic complexity ≤ 10

**Data:** 2026-07-22
**Status:** zatwierdzony projekt, gotowy do wykonania fazami
**Prompt wykonawczy:** `docs/superpowers/specs/2026-07-22-code-cleanup-prompt.md`

## Cel

Dwa twarde limity, egzekwowane maszynowo po zakończeniu prac:

1. **Każdy plik `.py` ≤ 600 linii** — kod i testy jednakowo.
2. **Każda funkcja CC ≤ 10** — bez wyjątków, bez `# noqa: C901`, bez podnoszenia progu.

Pliki `.js` pakietu wchodzą w zakres jako ostatnia, opcjonalna faza.

## Baseline

**Nie ufaj liczbom poniżej.** Zostały zmierzone 2026-07-22 na `9e28c90` i są tu wyłącznie po to, żeby pokazać skalę. W trakcie jednej sesji analitycznej (≈3 h) do `main` weszły dwa PR-y, które podniosły `resolve_step_target` z 15 na 20, `compile.py` z 897 na 1027 linii i dorzuciły dwa nowe pliki ponad limit. **Każda faza zaczyna się od własnego pomiaru.**

Polecenia pomiarowe:

```bash
git fetch && git status -sb          # najpierw: czy w ogóle jestem na aktualnym drzewie?

# UWAGA: zakres MUSI obejmować tests/. CI robi `ruff check .`, więc bramka
# obejmie testy — pomiar skanujący tylko guidebot_recorder/ jest ślepy na
# połowę repo i przepuści naruszenia aż do fazy 6.
uvx ruff check --isolated --select C901 \
    --config 'lint.mccabe.max-complexity=10' --output-format concise guidebot_recorder tests

for f in $(git ls-files 'guidebot_recorder/*.py' 'tests/*.py'); do
  n=$(wc -l < "$f"); [ "$n" -gt 600 ] && printf '%6d  %s\n' "$n" "$f"
done | sort -rn
```

Stan: **16 funkcji** ponad CC 10 — 15 w `guidebot_recorder/` plus **`_read_png` (CC 14) w `tests/integration/test_guide_select_reveal.py:119`**, przypisane do fazy 5. Ta szesnasta była niewidoczna, dopóki polecenie pomiarowe skanowało wyłącznie `guidebot_recorder/`.

**20 plików** ponad 600 linii — **4 kod** (`render.py` 3012, `recorder.py` 1328, `mux.py` 1300, `compile.py` 1027), **14 testów** (od 3615 do `test_cli.py` 601), **2 JS**.

Rozkład złożoności jest skrajnie nierówny i to determinuje podział na fazy:

| klasa | funkcje | charakter |
|---|---|---|
| monstra | `run_render` 97, `_render_step` 49, `capture_pages` 46, `run_compile` 39, `_compile_step` 36 | prawdziwa dekompozycja przez obiekt stanu |
| średnie | `compose_popup_video` 21, `resolve_step_target` 20, `_mux_tracks_for_timeline` 19, `mux_audio_tracks` 17, `_compose_floating` 16 | ekstrakcja czystych pomocników |
| drobne | `validate_compile_time` 15, `load_render_set` 14, `_result_from_payload` 14, `_compose_slide` 12, `render_set_output_paths` 12 | guard clauses, godziny pracy |

---

## Cztery ustalenia, które kształtują cały plan

### 1. Model liczenia ruffa — zweryfikowany eksperymentalnie

| konstrukcja | wkład |
|---|---|
| `if` / `elif` / `for` / `while` / `except` / `case` | +1 |
| **zagnieżdżony `def`** | **dokładnie tyle, ile wynosi jego własne CC** |
| `else`, `finally`, `with`, `assert` | 0 |
| ternary `a if b else c` | 0 |
| comprehension, `and` / `or` | 0 |

Uściślenie do wiersza o domknięciach: rodzic dostaje własne CC domknięcia, **bez dodatkowego +1**. Rodzic z dwoma domknięciami o CC 2 i 3 ma CC 6 (= 1 + 2 + 3), nie 8. Wcześniejsza wersja tego dokumentu mówiła „+1 i cała złożoność" — to zawyżało szacunki o jeden punkt na domknięcie.

Dwie konsekwencje praktyczne:

- **`match` nie pomaga.** `match` z trzema `case` kosztuje dokładnie tyle co `if/elif/elif`. Przepisywanie drabinek na `match` w celu zbicia metryki to praca zmarnowana.
- **Domknięcia to najtańsze punkty.** 17 z 97 punktów `run_render` i 8 z 19 punktów `_mux_tracks_for_timeline` to zagnieżdżone `def`-y. Przeniesienie ich na metody obiektu stanu daje te punkty przy zerowej zmianie zachowania.

### 2. Podział pliku zrywa monkeypatche — i to jest najgroźniejsza część operacji

Testy nie tylko importują z tych modułów; **podmieniają atrybuty na obiekcie modułu** (~60 miejsc w całym repo).

```python
render_module = importlib.import_module("guidebot_recorder.recorder.render")
monkeypatch.setattr(render_module, "detect_content_crop", forbidden)
```

Zweryfikowane zachowanie:

| wariant | patch dociera do `from .leaf import seam` | do `leaf.seam()` |
|---|---|---|
| zwykła fasada re-eksportująca | ❌ | ❌ |
| fasada z przekierowującym `__setattr__` † | ✅ | ✅ |
| wołanie przez obiekt modułu + przecelowany patch | — | ✅ |

† Działa tylko wtedy, gdy `__setattr__` **jawnie zapisuje wartość do `__dict__` podmodułów**, i wymaga podmiany klasy modułu w `sys.modules` po wykonaniu wszystkich przypisań modułowych. Pasywna fasada (`__getattr__`) nie robi tego wcale — `__getattr__` obsługuje odczyt, a monkeypatch wykonuje zapis. Wariant odrzucony (decyzja D2), wiersz zostaje wyłącznie po to, żeby nikt nie „uprościł" fasady w tę stronę, sądząc, że to bezpieczne.

Najgorsze są **ciche zerwania**: gdy test podmienia timeout na 0.3 s, a asercja brzmi `elapsed < 5.0`, to po zerwaniu patcha test dalej przechodzi i przestaje pilnować czegokolwiek. W samym `render.py` zidentyfikowano ≥4 takie przypadki (budżet wyszukiwania popupu ×7 testów, `cropdetect musi zostać ostatecznością` ×2).

**Przyjęte rozwiązanie** (zweryfikowane: `monkeypatch.setattr` na nieistniejącym atrybucie rzuca `AttributeError`):

> Fasada re-eksportuje to, co testy **importują**.
> Fasada świadomie **nie** re-eksportuje tego, co testy **podmieniają**.
> **Gdy nazwa należy do obu zbiorów — traktujemy ją jak podmienianą i nie re-eksportujemy.**

Wtedy każdy nieprzeniesiony patch wywala się natychmiast, z nazwą atrybutu w komunikacie. Nie istnieje cicha ścieżka.

**Rozstrzygnięcie przecięcia jest konieczne, nie teoretyczne.** Zmierzone dla `render.py`: 24 nazwy importowane, 20 podmienianych, **5 w obu zbiorach**:

```
_apply_timeline_edits   _assemble_audio_tracks   _pace_narration
_publish_render_artifacts   _render_step
```

Bez reguły rozstrzygającej agent wykonujący fazę 1 nie ma jak wybrać, a obie odpowiedzi są złe na różne sposoby: re-eksport gubi zabezpieczenie `AttributeError` dla ~15 miejsc podmiany najczęściej patchowanych nazw w całym suite; wstrzymanie wywala pięć plików testowych na `ImportError`. Wybieramy `ImportError` — **jest głośny i pojawia się przy zbieraniu testów**, zanim cokolwiek zdąży przejść. W tym samym commicie przecelowujemy zarówno import, jak i patch na moduł-właściciela.

Uzupełniająco — **reguła szwów** dla konsumentów wewnątrz pakietu:

```python
from . import ffmpeg          # obiekt modułu: późne wiązanie
ffmpeg._run([...])            # ✅ patchowalne przez ffmpeg._run

from .ffmpeg import _run      # ❌ wartość związana przy imporcie
_run([...])                   #    żaden patch tego nie dosięgnie
```

Reguła jest pilnowana **testem-strażnikiem na AST**, nie dyscypliną. Źródłem prawdy dla listy szwów w strażniku są **rzeczywiste cele podmiany w `tests/`**, wyliczane skanem, a nie lista wpisana na sztywno — inaczej strażnik zgnije przy pierwszym nowym szwie, czyli dokładnie wtedy, gdy jest potrzebny.

#### Niezmiennik, który zastępuje nieostre „przecelować na moduł-właściciela"

Zwrot „moduł-właściciel" jest dwuznaczny (miejsce **definicji** czy nowy dom **konsumenta**?) i w tej postaci prowadzi do cichej awarii. Obowiązuje sformułowanie jednoznaczne:

> **Test musi podmieniać ten moduł, którego globalne odczytuje konsument w momencie wywołania.**

Wybór jest **parą sprzężoną**, podejmowaną osobno dla każdej nazwy:

| konsument | test podmienia |
|---|---|
| zostaje przy `from X import nazwa` | moduł **konsumenta** |
| przepięty na `mod.nazwa(...)` | moduł **definiujący** |

Pomieszanie wariantów — podmiana w module definiującym, gdy konsument związał nazwę przy imporcie — **chybia po cichu**. Dlatego pierwszym artefaktem każdego podziału jest **tabela per nazwa**: podmieniana nazwa → funkcja(-e) konsumujące → moduł po podziale → nowy cel podmiany.

**Nazwa o konsumentach w dwóch modułach po podziale wymaga dwóch linii podmiany.** Jedna przestaje wystarczać, a druga ścieżka przechwytywania znika bez śladu. W `render.py` taką nazwą jest `Recorder` (konsumowany przez `run_render` **i** `_prepare_main_after_popup_close`) oraz `probe_frame_count`.

#### Reguła wstrzymywania dotyczy powierzchni testowej, nie produkcyjnej

Wcześniejsza wersja tej reguły mówiła po prostu „nie re-eksportuj tego, co testy podmieniają" — i była **błędna**, bo ignorowała konsumentów produkcyjnych. Dla `mux.py` wszystkie trzy podmieniane nazwy są importowane przez kod produkcyjny:

```
_run            ← video/timeline.py
_run_to_output  ← video/sfx.py, video/audiobed.py, video/timeline.py
probe_duration  ← video/__init__.py, recorder/render.py, video/timeline.py
```

Wstrzymanie ich z fasady bez dalszych działań **wywala pięć modułów produkcyjnych przy imporcie**. Poprawnie:

1. Reguła wstrzymywania rządzi powierzchnią **testową**.
2. Konsumentów produkcyjnych wstrzymanej nazwy przecelowujemy w tym samym commicie na **moduł definiujący** (`mux.ffmpeg`, `mux.probe`).
3. Jeżeli nazwa jest szwem testowym, konsument produkcyjny też musi wołać **przez obiekt modułu** — inaczej patch nie dosięgnie np. `video/timeline.py`.

#### Zmierzone przecięcia (importowane ∩ podmieniane)

| moduł | importowane | podmieniane | przecięcie |
|---|---|---|---|
| `recorder/render.py` | 24 | 20 | **5** — `_apply_timeline_edits`, `_assemble_audio_tracks`, `_pace_narration`, `_publish_render_artifacts`, `_render_step` |
| `video/mux.py` | 10 | 3 | **1** — `probe_duration` (wstrzymanie wymusza przecelowanie ~11 importów w ~10 plikach testowych **plus** konsumenci produkcyjni wyżej) |
| `recorder/compile.py` | 7 | 2 | **∅** — fasada bezpieczna; importy produkcyjne dotyczą wyłącznie nazw niepodmienianych |

### 3. Refaktoring systematycznie przenosi kod, którego nie pokrywają testy

To nie pech, tylko własność strukturalna: kod przenoszony przy zbijaniu complexity to gałęzie brzegowe — obsługa błędów, ścieżki awaryjne, retry — czyli dokładnie ten, który najtrudniej sprowokować w teście.

Zidentyfikowane luki, wszystkie w kodzie, który plan przenosi:

| miejsce | co jest niepokryte | dlaczego to boli |
|---|---|---|
| `render.py` P16→P17 | **zamiana kolejności kompozycji popupu i edycji czasu nie wywala żadnego testu** | daje film właściwej długości z popupem w złym momencie |
| `resolution.py` (precedencja odrzuceń) | linia wykonywana, **zachowanie niesprawdzane** — patrz niżej | render i compile mogłyby się rozjechać co do zamrożonego celu |
| `reasoner.py` (2 gałęzie odrzuceń) | całkiem niewykonywane; kolejne 2 wykonywane bez asercji na treści | |
| `scenario/render_set.py` (generyczny `except`) | gałąź redakcji sekretów | granica bezpieczeństwa |
| `recorder/render_set.py` (2 gałęzie) | ucieczka katalogu roboczego, kolizja workspace×workspace | |

**Reguła:** jeżeli faza przenosi gałąź bez pokrycia — test charakteryzujący jest pierwszym commitem tej fazy, przed dotknięciem kodu produkcyjnego.

**Reguła mocniejsza, wyprowadzona z wykonania fazy 0.** Dwa z powyższych punktów opisałem początkowo błędnie jako „linia nigdy niewykonywana", a okazały się wykonywane — i mimo to niezabezpieczone. W `resolution.py` wszystkie istniejące testy przechodzą przez tę gałąź celem `name="Nie ma", exact=True`, gdzie wariant ścisły i zrelaksowany zawodzą **identycznym komunikatem**; zamiana `elif rejection is None` na przypisanie bezwarunkowe zostawia cały suite zielony. W `reasoner.py` dwie gałęzie były osiągane przez `CodexReasoner.resolve` z gołym `pytest.raises(ValueError)`, który nie odróżnia sześciu różnych odrzuceń.

> Właściwe pytanie nie brzmi „czy ta linia się wykonuje", tylko **„czy istnieje zmiana tej linii, która zepsułaby zachowanie i przeszłaby przez suite"**. Coverage odpowiada na pierwsze i milczy na drugie. Asercje mają dotyczyć dokładnej treści (równość, nie `match=`), a test charakteryzujący jest ważny dopiero po zobaczeniu, że **czerwienieje po celowym zepsuciu**.

### 4. Repo porusza się szybciej, niż powstaje plan

Podczas jednej sesji analitycznej lokalne `main` okazało się 9 commitów w tyle (`git status` pokazywał „clean" — bo porównuje z lokalnym HEAD, nie ze zdalnym), a następnie doszedł kolejny PR. Trzy z siedmiu analiz czytały nieaktualne drzewo.

**Reguła:** każda faza zaczyna się od `git fetch && git status -sb`, a plan modułów weryfikuje się pomiarem, nie zaufaniem do tabeli w specu.

---

## Decyzje

| # | decyzja | uzasadnienie |
|---|---|---|
| D1 | Podział plików → pakiet z fasadą w `__init__.py` | zachowuje importy w testach bez zmian |
| D2 | Fasada nie re-eksportuje nazw podmienianych przez testy | zamienia ciche zerwania na `AttributeError` |
| D3 | Szwy testowe wołane przez obiekt modułu, pilnowane testem AST | czyni regułę samo-egzekwującą się |
| D4 | Zero `conftest.py` w `tests/` — pomocniki w jawnie importowanych `_*_helpers.py` | zachowuje istniejącą, świadomą własność „czytasz plik testowy i widzisz wszystko" |
| D5 | Bramki (`C901` + hook 600 linii) jako **ostatnia** faza dla Pythona | wcześniej blokowałyby własne PR-y |
| D6 | Testy charakteryzujące przed przenoszeniem niepokrytych gałęzi | patrz ustalenie 3 |
| D7 | `test_selects_js.py` **dzielony na 7** mimo narracyjnej struktury | decyzja właściciela repo; limit traktowany bezwyjątkowo |
| D8 | `.js` jako osobna, ostatnia, opcjonalna faza | inna klasa ryzyka (kolejność skryptów init jest load-bearing) |

**Koszt D7 i jego złagodzenie.** Docstring `test_selects_js.py` deklaruje, że kluczowy niezmiennik (shimowanie `<select>` nie rusza struktury DOM) jest *„asserted first and hardest"*, a pytest zbiera pliki alfabetycznie — podział odwróciłby tę kolejność. Łagodzenie: **plik z niezmiennikiem strukturalnym zachowuje oryginalną nazwę `test_selects_js.py`**, pozostałe sześć dostaje sufiksy (`test_selects_js_geometry.py` itd.). Znak `.` sortuje się przed `_`, więc kolejność „najważniejszy pierwszy" ocaleje. Komentarze odsyłające do sąsiedniej sekcji trzeba przy podziale przepisać na jawne odwołania do pliku.

---

## Fazy

Każda faza = jeden PR.

**Kolejność NIE jest dowolna.** Faza 0 poprzedza wszystko. Faza 6 musi być ostatnia dla Pythona (inaczej blokuje własne PR-y — patrz D5). Fazy 1 i 2 obie dotykają `render.py`, więc nie mogą iść równolegle: `_mux_tracks_for_timeline` z fazy 2 mieszka w pliku, który faza 1 zamienia w pakiet — po fazie 1 jego ścieżka jest inna. Fazy 4 (recorder) i 5 (testy) są niezależne od 1–3 i od siebie.

**Faza 5 NIE jest swobodna.** Dzieli pliki testowe „lustrzanie do podziału źródeł" — a lustro nie istnieje przed fazą 1, zaś odpowiednik `test_recorder_select.py` (1501 linii) wymaga fazy 4. Do tego faza 1 przepisuje dziesiątki miejsc podmiany w `test_render.py`, czyli w pliku, który faza 5 dzieli.

Zalecana sekwencja: **0 → 1 → 2 → 3 → 4 → 5 → 6**, przy czym 4 może wejść w dowolnym miejscu po 0.

**Faza 1 dzieli się na trzy PR-y, po jednym na plik źródłowy** — jest zbyt duża jako jeden. Kolejność rosnącego ryzyka:

| PR | cel | miejsca podmiany | przecięcie |
|---|---|---|---|
| **1a** | `mux.py` 1300 | 6 | 1 |
| **1b** | `compile.py` 1027 | 7 | 0 |
| **1c** | `render.py` 3012 | ~64 | 5 |

Trzy fasady są niezależne. Mały PR 1a ćwiczy **cały mechanizm** — fasadę, wstrzymywanie, przecięcie, strażnika AST, przepięcie szwów — przy mniej więcej dziesiątej części powierzchni podmian `render.py`. Strażnik AST powstaje w 1a i jest rozszerzany w kolejnych.

### Faza 0 — pomiar i siatka bezpieczeństwa (bez zmian produkcyjnych) — ✅ WYKONANA

Zrealizowana 2026-07-22, zmergowana jako PR #58: **+473 / −7 linii** w 5 plikach testowych (dwa commity — `7a932cb` plus `3d657aa` z poprawkami po recenzji), zero zmian w kodzie produkcyjnym.

Co zostało zabezpieczone:

- **kolejność `P16 → P17`** w `run_render` — z dowodem: po zamianie faz miejscami *wszystkie 124 istniejące testy w pliku przechodziły*. Nowy test asertuje kolejność wywołań **oraz ścieżki** (sama kolejność przepuściłaby edycję czasu czytającą surowe nagranie zamiast kompozytu).
- `DesktopOverlay` w asercji kolejności skryptów init — pozycja zweryfikowana wobec kodu: **między `slide` a `selects`**, nie na końcu.
- `reasoner.py` — 8 przypadków parametryzowanych na dokładną treść komunikatów, wołanych wprost na funkcji (nie przez `resolve`, który retryuje i zaciera różnice).
- `scenario/render_set.py` — redakcja sekretu, z asercją na `__suppress_context__` i pełnym renderze tracebacku (samo sprawdzenie `str(exc)` przepuściłoby usunięcie `from None`).
- `recorder/render_set.py` — ucieczka katalogu roboczego oraz kolizja workspace×workspace; ta druga okazała się **osiągalna przez realny manifest** (człon `.guidebot_video` w ścieżce wyjściowej).
- `resolution.py` — precedencja odrzuceń; pierwotna teza („gałąź nigdy niewykonywana") była błędna, realna luka leżała gdzie indziej (patrz ustalenie 3).

### Faza 1 — podział plików (`render.py`, `mux.py`, `compile.py`)

> **Ta faza NIE jest czysto mechaniczna.** Pierwsza wersja tego dokumentu tak ją opisywała i było to błędne. Przeniesienie kodu przez granicę modułu zrywa monkeypatche (ustalenie 2), więc **ciała `run_render`, `_render_step`, `run_compile` i `_compile_step` muszą zostać przepięte na wywołania przez obiekt modułu dla każdej podmienianej nazwy — łącznie z klasami (`Recorder`, `Overlay`, `SlideOverlay`) i stałymi (`_POPUP_REQUEST_LOOKUP_TIMEOUT`)**. To jedyne sankcjonowane odstępstwo od zasady „tylko przeniesienia" i trzeba je zaplanować, a nie odkryć w połowie.
>
> Faza obejmuje też edycję dziesiątek miejsc podmiany w `test_render.py` (3615 linii) i `test_mux.py` (1583). **To najbardziej ryzykowny PR całej serii**, nie rozgrzewka.

Przeniesienie samowystarczalnych grup pomocniczych do podmodułów. Logika bez zmian; `git diff -M` ma pokazywać przeniesienia wszędzie poza przepięciem szwów.

**Docelowy układ `recorder/compile/`** (PR 1b) — do zweryfikowania pomiarem, ale nie do wymyślania od zera w trakcie:

```
__init__.py   fasada (__all__ musi zostać bajtowo identyczne — test asertuje
              "install_selects" not in compile_module.__all__)
state.py      obiekty stanu pętli kompilacji
pages.py      stałe okna popupu, obserwacja stron, _wait_for_new_pages, _prepare_popup
cache.py      _load_prior_actions, compile_up_to_date, _fingerprint_matches, _can_reuse
describe.py   _short, _target_desc, _warn_absent, _resolve_url
step.py       _compile_step i jego pomocnicy
run.py        run_compile, run_compile_in_browser
```

Szwy: `write_compiled` (konsumowany w `run.py`) i `resolve_step_target` (w `step.py`). Przecięcie importowane ∩ podmieniane jest **puste**, więc fasada `compile.py` jest bezpieczna — wstrzymujemy tylko te dwie nazwy i przecelowujemy 9 miejsc w 2 plikach testowych, wszystkie padające głośno.

`compile.py` (1027 linii) wchodzi do tej fazy, bo **żadna inna go nie sprowadza pod limit**: faza 3 zbija jego complexity, ale dekompozycja przez obiekt stanu zwykle najpierw *dodaje* linie. Bez tego bramka z fazy 6 wywala się na pliku, którego nikt nie zaplanował dzielić — dokładnie to, czemu D5 ma zapobiegać.

```
recorder/render/            video/mux/
  __init__.py  (fasada)       __init__.py  (fasada)
  errors.py                   ffmpeg.py
  constants.py                probe.py
  tasks.py                    crop.py
  popup_detect.py             plan.py
  popup_crop.py               graph.py
  popup_session.py            compose.py
  pages.py                    floating.py
  visuals.py                  slide.py
  narration.py                tracks.py
  timeline.py
  audio.py
  reuse.py
  _step.py   (blob, faza 3)
  _run.py    (blob, faza 3)
```

`render/_run.py` zostaje ponad 600 linii do fazy 3 — to jest oczekiwane i musi być zapisane w opisie PR-a, żeby nikt nie uznał sprzątania za skończone.

Przy okazji: usuń martwy kod `_probe_fps` i `_probe_size` w `mux.py` (22 linie, zero wywołań w całym repo).

### Faza 2 — funkcje o umiarkowanej złożoności

Sześć funkcji, kolejność od najbezpieczniejszej:

1. `validate_compile_time` 15 → 6 — podział podyktowany komentarzem już obecnym w pliku; pełne pokrycie
2. `_result_from_payload` 14 → 9 — **najpierw** test parametryzowany na 3 niepokryte gałęzie odrzuceń
3. `render_set_output_paths` 12 → 2 — najpierw 2 brakujące przypadki
4. `load_render_set` 14 → 8 — najpierw test redakcji sekretu w generycznym `except`
5. `resolve_step_target` 20 → 9 — najpierw test udanej relaksacji `exact`
6. `_mux_tracks_for_timeline` 19 → 9 — ostatni; asyncio z ręcznym shield/drain, przenosić bajt w bajt

Plus cztery funkcje `mux.py`: `compose_popup_video` 21 → 3, `_compose_floating` 16 → 3, `_compose_slide` 12 → 3, `mux_audio_tracks` 17 → 1. Poprzedzone testem złotego filtergraphu (asercja na pełnym stringu `-filter_complex`), bo obecne testy sprawdzają głównie „renderuje się bez błędu".

### Faza 3 — pięć monstrów

Reguła nadrzędna: **najpierw obiekt stanu, potem ekstrakcja faz.** Odwrotna kolejność produkuje funkcje o ośmiu parametrach zwracające krotki, czyli przenosi złożoność do sygnatur.

**`run_render` 97 → 3.** Trzy obiekty o różnym czasie życia (`_RenderPlan` zamrożony, `_Clock` oś nagrania, `_Stage` co jest na ekranie), nie jeden god-object. Krytyczne: `last_freeze_frame` musi zostać polem obiektu, a callback `on_sfx` — **metodą związaną**, żeby zachować późne wiązanie dzisiejszego domknięcia. Przekazanie wartością zrywa je po cichu i przesuwa umiejscowienie dźwięków, nie zmieniając żadnej długości.

**`_render_step` 49 → 4.** To *dwie* dyspozycje na *dwóch różnych kluczach* (`kind` scenariusza, potem `cached.action` sidecara), relacja wiele-do-wielu. Pojedynczy rejestr na `kind` jest strukturalnie błędny. Zalecenie: łańcuch `if/elif` jednolinijkowych delegacji + **jawne `else: raise`** (dziś nieznana akcja nie robi nic i nie zgłasza błędu — to utajony błąd, do naprawy w osobnym commicie z własnym testem).

**`capture_pages` 46 → ≤8.** Niezmiennik `prev_shape` (aktualizowany **po** zbudowaniu strony) przestaje być komentarzem: para `(cursor, shape)` trafia do `_CursorTrail` z prywatnymi polami, gdzie odczyt i zapis są jednym wyrażeniem wołanym z pozycji argumentu. Nie da się wstawić instrukcji pomiędzy.

**`run_compile` 39 → ≤7, `_compile_step` 36 → ≤6.** Lustrzane do render. Nie unifikować z render w tym samym PR — patrz backlog.

**Ograniczenie wobec fazy 1:** funkcje modułowe, które testy podmieniają, **muszą pozostać funkcjami modułowymi**. Faza 1 przecelowuje ~15 miejsc podmiany na moduł-właściciela; zwinięcie tych funkcji w metody `_Clock`/`_Stage` zerwałoby te patche po raz drugi. Jeżeli któraś naprawdę powinna zostać metodą — przenieś także patche, w tym samym PR i świadomie.

**Uwaga o typach:** repo **nie ma type-checkera** (żadnej konfiguracji mypy/pyright w `pyproject.toml`, CI ani pre-commit). Osobne typy filmu poprawiają czytelność wywołań, ale **same z siebie niczego nie wymuszają w czasie wykonania**. Ochroną niezmiennika jest test z fazy 0. Jeśli chcemy realnego wymuszenia — trzeba albo dodać asercję `isinstance`, albo wprowadzić type-checkera jako osobną decyzję.

**Niezmienniki kolejności** mają po refaktoringu być *trudniejsze* do złamania:

| niezmiennik | ochrona |
|---|---|
| cursor/slide/desktop **przed** chrome.js | jedna funkcja `_install_page_scripts`, której ciałem *jest* kolejność, plus asercja runtime |
| kompozycja popupu **przed** edycją czasu | **test z fazy 0** (jedyna realna ochrona); typy `_RecordedFilm` → `_ComposedFilm` → `_VirtualFilm` jako czytelna pomoc |
| monotoniczność `last_freeze_frame` | jedyny czytelnik i jedyny pisarz wewnątrz `_Clock` |
| sonda nieobecności **przed** narracją | szew `_prepare_step` / `_execute_step` |

### Faza 4 — `recorder.py` (1328 → ~340)

Nie problem complexity (zero naruszeń), tylko god-class: 23 z 37 metod to napędzanie `<select>` (72% linii klasy).

```
recorder/recorder.py        ~340   (rdzeń + 2 delegatory + re-eksport)
recorder/select_driver.py   ~490   (choreografia, jedyna część stanowa)
recorder/select_errors.py   ~265   (SelectDriveError, OPTION_MISSING, 7 konstruktorów)
recorder/select_probe.py    ~205   (pytania do <select>, bezstanowe)
recorder/_js.py             ~190   (10 stałych ze skryptami)
overlay/geometry.py         +8     (_center_of z recorder.py:307 — zrywa cykl importów)
```

Dwa cięcia to za mało (`select_driver.py` wyszłoby ~660). Kształt: wąskie zależności (`page`, `frame`, `approach`, `animated`, `open_hold_ms`), **nie** wsteczna referencja do `Recorder`.

Krytyczny szczegół: `approach` przekazać jako **lambdę**, nie metodę związaną — test podmienia `rec._approach` na *instancji*, żeby próbkować geometrię listy po obu stronach każdego przesunięcia kursora.

`SelectDriveError.reason` — kontrakt `OPTION_MISSING` = krok opcjonalny może być pominięty, każdy inny powód musi zatrzymać przewodnik — pozostaje nienaruszony. Klasa przenosi się do `select_errors.py`, `recorder.py` ją re-eksportuje (7 miejsc importu).

### Faza 5 — testy (14 plików)

Podział lustrzany do podziału źródeł. Pomocniki do jawnie importowanych `_*_helpers.py`, **nigdy** `conftest.py`.

Trzy pułapki mechaniczne:

1. **Re-eksport fixture'a wymaga `# noqa: F401`** — ruff ma `select = ["F"]`, a `tests/**` ignoruje tylko `E501`. To najbardziej prawdopodobna awaria mechaniczna całego podziału.
2. **`pytestmark` nie dziedziczy się przez import pomocnika.** Każdy nowy plik musi przenieść swoje markery i `skipif` dosłownie. Zgubiony `skipif` zmienia to, co CI uruchamia, i nikt tego nie zauważy. Zalecenie: eksportować blok markerów z modułu pomocniczego jako stałą i pisać `pytestmark = FFMPEG` — jedna definicja, nadal jawny import.
3. Pliki, które po podziale **przestają potrzebować ffmpeg**, obsłużyć osobnym, recenzowalnym commitem — nie przy okazji podziału.

### Faza 6 — bramki

**Dopisz `C901` do istniejącej listy `select` i dodaj sekcję `mccabe`. Zostaw `ignore` i `per-file-ignores` dokładnie jak są** — usunięcie ich (co sugerował wcześniejszy, uproszczony zapis tej sekcji) daje 24 nowe naruszenia na niezwiązanych plikach: 3× UP040, 17× E501 w `reasoner.py`, 4× B008 w `cli.py`.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "C901"]   # ← dopisane C901
ignore = ["UP040"]                             # ← bez zmian
# [tool.ruff.lint.per-file-ignores] — bez zmian

[tool.ruff.lint.mccabe]                        # ← nowa sekcja
max-complexity = 10
```

Lista `per-file-ignores` nie może natomiast zyskać **żadnego wpisu dla C901**.

Ruff nie ma reguły na długość pliku — potrzebny lokalny hook pre-commit (`*.py`, próg 600, lista wyjątków pusta) plus ten sam pomiar w CI. Do tego `CLAUDE.md` (dziś nie istnieje) z zapisem, **dlaczego** `__init__.py` jest fasadą i dlaczego nie wolno re-eksportować nazw podmienianych w testach — inaczej następna sesja to „uprości".

### Faza 7 — `.js` (opcjonalna)

`selects/selects.js` 1427, `overlay/cursor.js` 792. Podział wymaga sklejania po stronie Pythona (wzorzec już istnieje: `shape_prelude()`). Rozszerzyć hook o `*.js` dopiero po tej fazie.

---

## Reguły przekrojowe

Obowiązują w każdej fazie:

1. **Zielony suite po każdym commicie** — `uv run pytest -m "not network"`. Porównuj też **czas przebiegu** z baseline'em: zerwany patch timeoutu objawia się spowolnieniem, nie czerwienią.
2. **Jeden commit = jedna operacja.** Przeniesienie kodu i zmiana zachowania nigdy w tym samym commicie.
3. **Komentarze podróżują z kodem.** Ten kod ma wyjątkowo gęste komentarze *dlaczego*. Komentarz idący za funkcją → do jej nowego modułu. Komentarz opisujący relację *między* modułami → do docstringa modułu. Odwołania w stylu „patrz niżej" i „w `render.py`" trzeba przepisać — po podziale są fałszywe.
4. **Zakaz fałszywych zwycięstw.** Nie liczy się jako spełnienie celu: `# noqa: C901`; jednolinijkowy pomocnik wołany z jednego miejsca; przepisanie `if/elif` na `match` (metryka się nie zmieni); słownik lambd równie nieczytelny co drabinka.
5. **Docstring modułu obowiązkowy** — co tu jest i dlaczego akurat tu.
6. **Funkcja nieredukowalna to legalny wynik.** Jeśli zejście poniżej 10 wymagałoby pogorszenia czytelności — zgłoś to jako pytanie projektowe, nie tłum po cichu.

---

## Backlog (świadomie poza zakresem sprzątania)

Znalezione podczas analizy. Każde wymaga zmiany zachowania albo unifikacji — mieszanie tego z przenoszeniem kodu daje diff nie do zrecenzowania.

**Utajone błędy:**
- Dyspozytor akcji w `_render_step` nie ma `else` — nieznana akcja sidecara nie robi nic i nie zgłasza błędu.
- `_probe_fps`, `_probe_size` w `mux.py` — martwy kod (usuwany w fazie 1 jako wyjątek: usunięcie martwego kodu jest bezpieczne i lokalne).

**13 duplikacji compile ↔ render ↔ capture**, w kolejności rosnącego ryzyka:
- niskie: `_resolve_url` (3 kopie), stałe okna wykrywania popupu (2 kopie), domknięcie `step_banner` (5 kopii)
- średnie: protokół pomijania gałęzi (3 ręczne implementacje), lejek `pause_on_error` (4 kopie), `_unexpected_pages`, `_prepare_popup`
- wysokie: drabiny akcji i kroków bezcelowych — tu compile i render **naprawdę** się różnią; unifikować dopiero po ustabilizowaniu fazy 3

**Inne:**
- `guide/guide.py` powiela podzbiór kolejności skryptów init — po fazie 3 powinien wołać `_install_page_scripts`.
- `README.md` 791 linii, duplikuje `docs/`.
