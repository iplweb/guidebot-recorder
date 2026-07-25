# Obsługa popupów w `guidebot guide` (PDF)

Data: 2026-07-25

## Problem

`guide` odrzuca każdy scenariusz, w którym jakiś krok otwiera nowe okno:

```
BŁĄD: scenariusze z popupem nie są obsługiwane w `guide` v1 (krok otwiera nowe okno)
```

Blokada siedzi w `guide/prolog.py` (`scan_for_blockers`) i wyzwala ją zamrożona
flaga `opens_popup: true`. Dotyczy to całej klasy scenariuszy, których sednem
jest logowanie — a więc dokładnie tych, dla których przewodnik krok-po-kroku ma
największy sens.

Blokada nie jest kaprysem: `guide` nie ma **żadnego** cyklu życia okien.
`_Capture.page` jest ustawiane raz z okna głównego i każde z pięciu wywołań
`_screenshot(cap.page, …)` (`replay.py:216, 268, 328, 396, 403`) fotografuje
właśnie je. Krok działający na popupie szukałby swojego celu w oknie głównym
i padał z niezgodnością tożsamości — kilka kroków za późno, w niezrozumiałym
miejscu. Odrzucenie z góry było uczciwsze niż taka awaria.

## Rozwiązanie

Zamiast dokładać wykrywanie okien, zmieniamy jedno pojęcie: `_Capture.page`
przestaje znaczyć „okno główne", a zaczyna znaczyć **okno aktywne**. Pętla
przechwytywania nie dowiaduje się o tym niczego — te same pięć wywołań
`_screenshot(cap.page, …)` fotografuje inną wartość.

### Cykl życia sterowany sidecarem

Popupu nie wykrywamy heurystycznie. `compile` już zamraża `opens_popup: true`
(`recorder/compile/run.py:365`), więc:

* krok `click` z tą flagą owija kliknięcie w `context.expect_page()`
  i po powrocie przełącza aktywną parę `(page, recorder)` na nowe okno;
* krok `closeWindow` zamyka popup i przywraca parę okna głównego.

Świadomie **nie** sięgamy po `render/popup_detect.py` ani `render/popup_crop.py`
(789 linii). To maszyneria wideo: wykrywanie momentu z kwantem ciszy, przejścia
float/slide, kadrowanie klatek. PDF nie ma osi czasu, więc nie ma czego kadrować.

Popup dostaje własny `Recorder` — bez `frame=`, bo nie żyje w iframie powłoki
chrome. Pasek chrome na popupie zostaje sterowany istniejącym `cfg.popup.is_bare`,
które `run_guide` już przekazuje do `Chrome(...)`.

`closeWindow` przestaje być komendą wyłącznie narracyjną. Dziś siedzi
w `NARRATION_ONLY_KINDS` z komentarzem „popup bookkeeping the guide already
rejects" — dostaje własny rodzaj strony i własną fazę, bo od teraz **wykonuje**
pracę w przeglądarce.

### Płótno — w CSS, nie w obrazie

Zrzut popupu zostaje w naturalnym rozmiarze okna. Wyśrodkowanie na płótnie
o proporcjach okna głównego robi `layout.py`: bez przetwarzania obrazu i bez
nowej zależności.

Jeden haczyk, i to on decyduje o kształcie zmiany. Dziś SVG z adnotacjami leży
`inset: 0` nad `.shot`, a `img` wypełnia ten box — więc oba układy współrzędnych
się pokrywają. Gdy obrazek przestanie wypełniać `.shot`, adnotacje rozjadą się
o wielkość marginesu. Dlatego `img` i `svg` trafiają do wewnętrznego `.plate`
o rozmiarze obrazka, wyśrodkowanego w `.shot`. Dla stron nieletterboxowanych
`.plate` ma `width: 100%` i renderuje się identycznie jak dotąd.

`GuidePage` dostaje `canvas_size` obok istniejącego `screenshot_size`.
Letterboxing włącza się wtedy i tylko wtedy, gdy oba są znane i różne — czyli
sam fakt „to jest popup" nie jest nigdzie zapisywany jako flaga. Rozmiar
wystarcza.

## Obsługa błędów

Popup, który nie pojawi się mimo `opens_popup: true`, to twardy `GuideError`
z `plik:linia` i fragmentem YAML — tak jak każdy inny niespełniony namiar
w `guide`. Bez cichego pomijania: zamrożona flaga mówi, że okno *ma* się otworzyć,
a PDF bez tej strony byłby przewodnikiem, który gubi połowę instrukcji.

Wywołanie `enter_popup` bez wstrzykniętej fabryki `Recorder`-ów jest błędem
programisty, nie scenariusza, więc leci `RuntimeError`.

## Testy

Pakiet ma własny strażnik szwów (`tests/unit/guide/test_capture_seams.py`),
który wymaga, by nazwa patchowana na `capture` była wołana z `capture`. Nowy
przełącznik okien mieszka na `_Capture`, więc go nie dotyczy — ale testy nie
mogą go obchodzić name-importem w siostrzanym module.

Nowe przypadki:

* przejście na popup po kroku z `opens_popup` i powrót na `closeWindow`;
* zrzuty po przełączeniu pochodzą z popupu, nie z okna głównego;
* ślad kursora zeruje się na każdej zmianie okna (strzałka między oknami nie ma sensu);
* brak popupu mimo flagi → `GuideError` z `plik:linia`;
* `render_html` letterboksuje wtedy i tylko wtedy, gdy `canvas_size ≠ screenshot_size`,
  a adnotacje pozostają w układzie obrazka;
* `scan_for_blockers` przestaje odrzucać popupy, nadal odrzuca nieznane komendy
  i nierozwiązane kroki obowiązkowe.

## Zakres

Zmiana dotyczy wyłącznie `guidebot_recorder/guide/`. Nie ruszamy `render`,
przejść float/slide ani kadrowania wideo.

Pliki: `prolog.py`, `replay.py`, `capture.py`, `model.py`, `layout.py`, `guide.py`.
Wszystkie zostają grubo poniżej limitu 600 linii; żadna funkcja nie zbliża się
do CC 10.
