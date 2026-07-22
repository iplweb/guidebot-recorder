# Prompt wykonawczy: sprzątanie kodu guidebot-recorder

Projekt: `docs/superpowers/specs/2026-07-22-code-cleanup-design.md`

Każda faza = jeden PR = jedno wklejenie. **Preambuła obowiązuje w każdej fazie** — wklej ją razem z fazą.

**Kolejność nie jest dowolna:** `0 → 1 → 2 → 3 → 6`. Fazy 4 i 5 wchodzą w dowolnym miejscu po 0. Faza 6 **musi** być ostatnia dla Pythona. Fazy 1 i 2 obie dotykają `render.py` i nie mogą iść równolegle.

**Status: Faza 0 jest wykonana** (gałąź `cleanup/faza-0-siatka-bezpieczenstwa`). Zaczynaj od Fazy 1.

---

## PREAMBUŁA (dołącz do każdej fazy)

````text
Pracujesz w repo guidebot-recorder (Python 3.12, pakiet `guidebot_recorder`).
Sprzątasz kod pod dwa twarde limity: każdy plik .py ≤ 600 linii, każda funkcja
cyclomatic complexity ≤ 10. Projekt całości: docs/superpowers/specs/2026-07-22-code-cleanup-design.md

ZANIM COKOLWIEK ZROBISZ

1. `git fetch && git status -sb`. To repo porusza się szybko — w ciągu jednej
   sesji analitycznej weszły dwa PR-y, a lokalne main było 9 commitów w tyle
   przy `git status` pokazującym "clean". Czysty katalog roboczy NIE znaczy
   aktualny. Jeśli jesteś w tyle: `git pull --ff-only`.
2. Załóż gałąź: `git switch -c cleanup/<faza>`.
3. Zmierz stan sam. Nie ufaj żadnym liczbom w specu ani w tym prompcie —
   są ilustracją skali, nie kontraktem:

   uvx ruff check --isolated --select C901 \
       --config 'lint.mccabe.max-complexity=10' --output-format concise guidebot_recorder

   for f in $(git ls-files 'guidebot_recorder/*.py' 'tests/*.py'); do
     n=$(wc -l < "$f"); [ "$n" -gt 600 ] && printf '%6d  %s\n' "$n" "$f"
   done | sort -rn

4. Zapisz baseline: `uv run pytest -m "not network" -q` — wynik ORAZ CZAS.
   Czas jest częścią baseline'u, nie ciekawostką (patrz: pułapka szwów).

MODEL LICZENIA COMPLEXITY (zweryfikowany, nie zgaduj)

  +1: if, elif, for, while, except, case
  zagnieżdżony `def`: rodzic dostaje DOKŁADNIE własne CC domknięcia (bez
      dodatkowego +1). Rodzic z domknięciami o CC 2 i 3 ma CC 6 = 1+2+3.
   0: else, finally, with, assert, ternary, comprehension, and/or

  Dwa wnioski: (a) `match` NIE obniża CC — trzy `case` kosztują tyle co trzy
  `elif`, więc przepisywanie drabinek na `match` to praca zmarnowana;
  (b) domknięcia to najtańsze punkty — przeniesienie zagnieżdżonego `def`
  na metodę obiektu stanu to czysty zysk bez zmiany zachowania.

PUŁAPKA SZWÓW TESTOWYCH — przeczytaj, zanim podzielisz jakikolwiek plik

  ~60 miejsc w testach podmienia atrybuty na obiekcie modułu:
      monkeypatch.setattr(render_module, "detect_content_crop", fake)

  Po podziale pliku funkcja-czytelnik mieszka w innym module i rozwiązuje
  nazwę ze SWOJEGO __dict__. Patch trafia w próżnię. Część takich zerwań jest
  CICHA: gdy test podmienia timeout na 0.3 s, a asercja brzmi `elapsed < 5.0`,
  to po zerwaniu patcha test dalej przechodzi i przestaje pilnować czegokolwiek.

  OBOWIĄZUJĄCA REGUŁA:
    Fasada (__init__.py) re-eksportuje to, co testy IMPORTUJĄ.
    Fasada świadomie NIE re-eksportuje tego, co testy PODMIENIAJĄ.
    GDY NAZWA JEST W OBU ZBIORACH -> traktuj jak podmienianą, NIE re-eksportuj,
    i w tym samym commicie przecelowa zarówno import, jak i patch.

  To nie jest przypadek teoretyczny. Zmierzone dla render.py: 24 nazwy
  importowane, 20 podmienianych, 5 W OBU ZBIORACH:
      _apply_timeline_edits  _assemble_audio_tracks  _pace_narration
      _publish_render_artifacts  _render_step
  Wstrzymanie ich wywala pięć plików testowych na ImportError PRZY ZBIERANIU
  TESTÓW — głośno i natychmiast. Re-eksport gubiłby zabezpieczenie dla ~15
  miejsc podmiany najczęściej patchowanych nazw w suite. Wybieramy ImportError.

  Zanim zaczniesz, policz to przecięcie sam dla swojego modułu.

  Dzięki temu nieprzeniesiony patch daje natychmiastowy AttributeError
  (zweryfikowane: monkeypatch.setattr na nieistniejącym atrybucie rzuca).
  Nie ma cichej ścieżki. To jest cecha, nie niedopatrzenie — NIE "napraw" jej
  przez dodanie brakującego re-eksportu.

  Wewnątrz pakietu szwy woła się przez obiekt modułu:
      from . import ffmpeg
      ffmpeg._run([...])        # ✅ patchowalne
      # NIE: from .ffmpeg import _run; _run([...])   ❌ związane przy imporcie

  Zanim podzielisz plik, wypisz jego szwy:
      grep -rn 'setattr(.*<nazwa_modułu>' tests/
      grep -rn '"guidebot_recorder\.<ścieżka>\.' tests/

REGUŁY, KTÓRE OBOWIĄZUJĄ ZAWSZE

1. Zielony suite po KAŻDYM commicie: `uv run pytest -m "not network"`.
   Porównuj też czas z baseline'em — zerwany patch timeoutu objawia się
   spowolnieniem, nie czerwienią.
2. Jeden commit = jedna operacja. Przeniesienie kodu i zmiana zachowania
   NIGDY w tym samym commicie.
3. Komentarze podróżują z kodem. Ten kod ma wyjątkowo gęste komentarze
   "dlaczego" (kolejność skryptów init, oś nagrania vs wirtualna, trzy poziomy
   popup crop) — one niosą wiedzę, nie szum. Komentarz za funkcją → do jej
   nowego modułu. Komentarz o relacji MIĘDZY modułami → do docstringa modułu.
   Odwołania "patrz niżej" i "w render.py" MUSISZ przepisać — po podziale
   są fałszywe.
4. ZAKAZ FAŁSZYWYCH ZWYCIĘSTW. Nie liczy się jako spełnienie celu:
   - `# noqa: C901` ani podniesienie progu
   - jednolinijkowy pomocnik wołany z jednego miejsca, żeby przerzucić gałąź
     przez granicę funkcji
   - przepisanie if/elif na match (metryka się nie zmieni)
   - słownik lambd równie nieczytelny co drabinka
5. Każdy nowy moduł dostaje docstring: co tu jest i dlaczego akurat tu.
6. Funkcja nieredukowalna to LEGALNY wynik. Jeśli zejście poniżej 10
   wymagałoby pogorszenia czytelności — zatrzymaj się i zgłoś to jako pytanie
   projektowe. Nie tłum po cichu.
7. Nie zmieniaj treści komunikatów błędów. Są po polsku, starannie
   sformułowane, i część z nich testy dopasowują regexem.

RAPORTUJ UCZCIWIE. Jeśli test padł — pokaż wyjście. Jeśli czegoś nie zrobiłeś —
powiedz. Jeśli plik po Twojej fazie nadal przekracza limit, bo tak zaplanowano —
napisz to wprost w opisie PR-a, żeby nikt nie uznał sprzątania za skończone.
````

---

## FAZA 0 — pomiar i siatka bezpieczeństwa ✅ WYKONANA 2026-07-22

Zrealizowana na gałęzi `cleanup/faza-0-siatka-bezpieczenstwa`: +378 linii w 5 plikach testowych, zero zmian produkcyjnych. Sekcja zostaje jako zapis tego, co i dlaczego zabezpieczono — przydaje się przy czytaniu kolejnych faz.

Kluczowy wynik: po zamianie faz `P16`/`P17` miejscami **wszystkie 124 istniejące testy przechodziły**. Kolejności pilnował wyłącznie komentarz.

Bez zmian produkcyjnych. To najważniejsza faza: buduje dowód, na którym opierają się wszystkie następne.

````text
[PREAMBUŁA]

ZADANIE: przykryj testami gałęzie, które kolejne fazy będą przenosić, a których
dziś nie wykonuje żaden test. Nie zmieniasz ani jednej linii kodu produkcyjnego.

Kontekst: refaktoring systematycznie przenosi kod brzegowy — obsługę błędów,
ścieżki awaryjne, retry — czyli dokładnie ten, który testy pokrywają najsłabiej.
Cztery z poniższych to granice bezpieczeństwa (redakcja sekretów, kontrakt
walidacji).

1. NAJWAŻNIEJSZY: kolejność P16 → P17 w run_render.
   Kompozycja popupu działa na osi NAGRANIA, edycja czasu przenosi na oś
   WIRTUALNĄ. Zamiana kolejności daje film właściwej długości z popupem w złym
   momencie — i NIE WYWALA DZIŚ ŻADNEGO TESTU. Wszystkie kontrole poniżej
   porównują długość z modelem, czyli model sam ze sobą.

   Napisz test, który opakowuje compose_popup_video i _apply_timeline_edits,
   zapisuje kolejność wywołań ORAZ ścieżki, i asertuje:
     - kolejność == ["compose", "edit"]
     - wejście edycji == wyjście kompozycji (edycja konsumuje kompozyt, nie surowy webm)
   Scenariusz musi mieć popup ORAZ co najmniej jedno zamrożenie klatki.

   DOWÓD: celowo zamień kolejność w kodzie, potwierdź że test czerwienieje,
   cofnij. Bez tego kroku nie wiesz, czy test cokolwiek pilnuje.

2. resolution.py — gałąź udanej relaksacji `exact` (dziś nigdy nie wykonana).
3. reasoner.py — trzy gałęzie odrzuceń w arm-ie error (cały kontrakt).
   Funkcja jest synchroniczna i czysta, bierze zwykły dict — test parametryzowany,
   ~15 linii. NIE testuj tego przez CodexReasoner.resolve (retryuje dwukrotnie
   i zaciera różnicę między komunikatami).
4. scenario/render_set.py — generyczny `except` przy ładowaniu wariantu.
   To granica redakcji sekretów: asertuj, że sekret NIE pojawia się w komunikacie.
5. recorder/render_set.py — ucieczka katalogu roboczego poza --output-dir
   oraz kolizja workspace×workspace. Jeśli druga okaże się nieosiągalna przez
   publiczne API — napisz to wprost zamiast naginać test.
6. Dodaj DesktopOverlay do asercji kolejności skryptów init w test_render.py
   (dziś asercja pokrywa overlay/slide/selects/chrome, ale nie desktop).

KRYTERIUM GOTOWE: wszystkie nowe testy zielone; dla punktu 1 udokumentowany
dowód, że celowe zepsucie je wywala.
````

---

## FAZA 1 — podział plików (`render.py` 3012, `mux.py` 1300, `compile.py` 1027)

````text
[PREAMBUŁA]

ZADANIE: zamień render.py, mux.py i compile.py na pakiety, przenosząc
samowystarczalne grupy pomocnicze.

UWAGA — TA FAZA NIE JEST CZYSTO MECHANICZNA I JEST NAJRYZYKOWNIEJSZYM PR-em
CAŁEJ SERII. Nie daj się zwieść słowu "ekstrakcja":

  Przeniesienie kodu przez granicę modułu zrywa monkeypatche. Żeby patche
  przeżyły, ciała run_render / _render_step / run_compile / _compile_step MUSZĄ
  zostać przepięte na wywołania przez OBIEKT MODUŁU dla KAŻDEJ podmienianej
  nazwy — łącznie z KLASAMI (Recorder, Overlay, SlideOverlay) i STAŁYMI
  (_POPUP_REQUEST_LOOKUP_TIMEOUT). Reguła szwów z preambuły dotyczy ich tak
  samo jak funkcji.

  To jedyne sankcjonowane odstępstwo od "tylko przeniesienia". Poza nim
  `git diff -M` ma pokazywać przeniesienia, nie przepisania.

  Faza obejmuje też edycję dziesiątek miejsc podmiany w test_render.py (3615
  linii) i test_mux.py (1583). Zaplanuj to, nie odkrywaj w połowie.

Poza przepięciem szwów NIE zmieniaj logiki run_render, _render_step,
run_compile, _compile_step ani capture_pages — ich dekompozycja to faza 3.

compile.py (1027 linii) jest w tej fazie, bo ŻADNA INNA nie sprowadza go pod
600: faza 3 zbija jego complexity, ale dekompozycja przez obiekt stanu zwykle
najpierw DODAJE linie. Bez tego bramka z fazy 6 wywali się na pliku, którego
nikt nie zaplanował dzielić.

Docelowa struktura (zweryfikuj pomiarem, popraw jeśli trzeba):

  recorder/render/                     video/mux/
    __init__.py   fasada                 __init__.py   fasada
    errors.py     RenderError,           ffmpeg.py     _run, _run_to_output, biny
                  _OptionalAbsent        probe.py      probe_duration, _probe_all
    constants.py  budżety czasowe        crop.py       cropdetect, teardown tail
    tasks.py      _discard_pending       plan.py       PopupPlan, walidacja okna
    popup_detect.py  window.open         graph.py      wspólne fragmenty filtergraphu
    popup_crop.py    łańcuch cropu       compose.py    compose_popup_video
    popup_session.py cykl życia stron    floating.py   _compose_floating
    pages.py      która strona żyje      slide.py      _compose_slide
    visuals.py    warstwy wizualne       tracks.py     mux, mux_audio_tracks, fade
    narration.py  zegar narracji
    timeline.py   edycja czasu
    audio.py      podkłady, publikacja
    reuse.py      kontrakt sidecara
    _step.py / _run.py   (bloki na fazę 3)

KOLEJNOŚĆ WEWNĄTRZ FAZY:
  1. errors.py PIERWSZE i z zerowymi importami wewnętrznymi — RenderError jest
     podnoszony niemal wszędzie; zostawienie go przy run_render tworzy cykl
     totalny.
  2. Potem liście (constants, tasks, ffmpeg, probe), potem warstwy wyższe.

FASADA: zastosuj regułę z preambuły. Zanim ją napiszesz, wypisz osobno:
  (a) nazwy IMPORTOWANE z tego modułu → re-eksportuj, wpisz do __all__
      (ruff ma F401; bez __all__ posypie się na ~80 nazwach)
  (b) nazwy PODMIENIANE w testach → NIE re-eksportuj; zamiast tego przecelowa
      patche w testach na moduł-właściciela
Uwaga na przypadek szczególny: test podmienia "…render.os.replace", więc fasada
musi wiązać `os`, mimo że sama go nie używa.

DODAJ TEST-STRAŻNIK (AST): sprawdza, że żaden moduł w pakiecie nie robi
`from .x import <szew>` dla nazw będących szwami testowymi — mają być wołane
przez obiekt modułu. Bez tego reguła zgnije przy pierwszej edycji.

PRZY OKAZJI: usuń _probe_fps i _probe_size z mux.py — martwy kod, zero wywołań
w całym repo (zweryfikuj grepem, zanim usuniesz).

OCZEKIWANY WYNIK: render/_run.py NADAL ponad 600 linii — to jest zaplanowane.
Napisz to w opisie PR-a.
````

---

## FAZA 2 — funkcje o umiarkowanej złożoności

````text
[PREAMBUŁA]

ZADANIE: sprowadź poniższe funkcje do CC ≤ 10. Kolejność jest celowa —
od najbezpieczniejszej. Każda funkcja = osobny commit.

  1. resolver/validate.py       validate_compile_time      15 → ~6
     Podział jest już opisany komentarzem w pliku ("checks every action shares"
     vs "gates keyed to an action that touches the page"). Kod ma dogonić
     komentarz. Pełne pokrycie testami — najbezpieczniejsza z całej szóstki.

  2. resolver/reasoner.py       _result_from_payload       14 → ~9
     NAJPIERW test z fazy 0. Jeden pomocnik na arm error, nie dwa —
     kontrakt inputText to dwie połowy jednej reguły, rozdzielenie jednej
     z nich daje asymetrię za jeden punkt.

  3. recorder/render_set.py     render_set_output_paths    12 → ~2
     Funkcja robi dwie rzeczy: rozwiązuje ścieżki wariantu, potem sprawdza
     kolizje w zebranym zbiorze. Druga połowa nie patrzy już na wariant.

  4. scenario/render_set.py     load_render_set            14 → ~8
     Wydziel kontrakt configu i ładowanie wariantu. NIE wydzielaj bloku kolizji
     — czyta i pisze trzy zbiory należące do pętli; wyniesienie go oznacza
     7-parametrowy pomocnik. To gorszy kod dla metryki, która i tak jest
     spełniona bez tego.

  5. resolver/resolution.py     resolve_step_target        20 → ~9
     NAJPIERW test z fazy 0. Uwaga na kontrakt zwrotu pomocnika od relaksacji:
     przy nieudanej relaksacji musi zwrócić ORYGINALNY cel i ORYGINALNY werdykt
     (dzisiejszy kod odrzuca werdykt zrelaksowany). Pomyłka tutaj jest cicha.

  6. recorder/render.py         _mux_tracks_for_timeline   19 → ~9
     OSTATNIA. 8 z 19 punktów to dwa domknięcia; NIE wynoś ich na poziom modułu
     (zamkniętych jest na czterech zmiennych, wyszłoby 6 parametrów przez
     asyncio.to_thread). Wystarczą dwie ekstrakcje: guard narracji i drain.
     To asyncio z ręcznym shield/drain i czasem życia TemporaryDirectory
     zależnym od niego — przenoś bajt w bajt, nie "porządkuj".

  Plus cztery z video/mux.py:
     compose_popup_video 21→~3, _compose_floating 16→~3,
     _compose_slide 12→~3, mux_audio_tracks 17→~1

     POPRZEDŹ TESTEM ZŁOTEGO FILTERGRAPHU: obecne testy sprawdzają głównie
     "renderuje się bez błędu" i nie złapią filtergraphu poprawnego
     składniowo a błędnego semantycznie. Użyj istniejącego szpiega
     _capture_filtergraph, zaasertuj pełny string dla macierzy:
     cut × {pre+tail, bez pre, bez tail}, float × {shadow, blur, hold_open,
     crop/None}, slide × {hold_open, slide_ms=0}.
     Wtedy cały refaktoring weryfikuje się równością stringów.

     _compose_floating i _compose_slide są w ~65% identyczne linia w linię
     (zdiffuj przed rozpoczęciem). Wyciągnięcie tej wspólnoty to jednocześnie
     poprawka rozmiaru i złożoności.
     ALE: NIE łącz ramp progresji float i slide. Wyglądają tak samo, różnią się
     jednym max(0, ...) — a to jest różnica między poprawnym a przesuniętym
     w złą stronę przejściem. Renderuje się bez błędu w obu wypadkach.
````

---

## FAZA 3 — pięć monstrów

````text
[PREAMBUŁA]

ZADANIE: run_render 97, _render_step 49, capture_pages 46, run_compile 39,
_compile_step 36 → wszystkie ≤ 10.

To najtrudniejsza faza. Wymaga fazy 0 (test kolejności P16→P17) i najlepiej
po fazie 1.

REGUŁA NADRZĘDNA: NAJPIERW OBIEKT STANU, POTEM EKSTRAKCJA FAZ.
Odwrotna kolejność produkuje funkcje o ośmiu parametrach zwracające krotki —
to przenosi złożoność do sygnatur zamiast ją usuwać. Zanim cokolwiek wytniesz,
wypisz inwentarz zmiennych mutowanych w więcej niż jednym miejscu.

run_render → trzy obiekty o RÓŻNYM czasie życia, nie jeden god-object:
  _RenderPlan  (zamrożony; wszystko ustalone zanim istnieje przeglądarka)
  _Clock       (oś nagrania: zamrożenia, umiejscowienia dźwięku i narracji)
  _Stage       (co jest teraz na ekranie: strony, popup, warstwy, karta)

  PUŁAPKA KRYTYCZNA: `last_freeze_frame` jest dziś czytany przez domknięcie
  przekazane jako Recorder(on_sfx=...), czyli wykonywane PIĘTRO NIŻEJ i
  czytające wartość w momencie wywołania. Przekazanie go WARTOŚCIĄ do funkcji
  kroku zrywa to po cichu: wszystkie kontrole długości nadal przechodzą,
  przesuwa się tylko UMIEJSCOWIENIE dźwięków i narracji.
  Rozwiązanie strukturalne: pole na _Clock + przekazanie METODY ZWIĄZANEJ
  jako callbacku. Metoda odczytuje self przy wywołaniu — dzisiejsza semantyka
  z konstrukcji, nie z dyscypliny.

  DROBNY ZYSK PO DRODZE: `card_active` i `active_card` to jedna zmienna
  (`card_active == (active_card is not None)` w każdym z 10 miejsc zapisu;
  kod sam to stwierdza asercją). Scal w `card: Card | None` — osobny commit.

_render_step → to DWIE dyspozycje na DWÓCH różnych kluczach:
  A: na `kind` scenariusza (say/desktop/slide/closeWindow/navigate/wait/scroll)
  B: na `cached.action` sidecara (click/hover/type/select/highlight/waitFor)
  Relacja jest wiele-do-wielu (`teach` zamraża się na dowolną z akcji;
  `wait` rozdziela się między A i B zależnie od requires_target()).
  POJEDYNCZY REJESTR NA `kind` JEST STRUKTURALNIE BŁĘDNY — każdy handler
  musiałby powtórzyć guardy zamrożonej akcji.
  Zalecenie: łańcuch if/elif jednolinijkowych delegacji + kontekst jako JEDEN
  argument (nie 8-argumentowy uniwersalny protokół — przy nim `say`, który nie
  potrzebuje niczego, staje się mniej czytelny niż dziś).

  ZNALEZIONY UTAJONY BŁĄD: dyspozycja B nie ma `else`. Nieznana akcja sidecara
  nie robi nic i nie zgłasza błędu. Napraw to — ale W OSOBNYM COMMICIE,
  z własnym testem, bo to zmiana zachowania.

capture_pages → niezmiennik prev_shape (aktualizowany PO zbudowaniu strony)
  ma przestać być komentarzem. Para (cursor, shape) → obiekt z PRYWATNYMI
  polami, gdzie odczyt-i-zapis są JEDNYM wyrażeniem, wołanym z pozycji
  argumentu przy budowaniu strony. Wtedy nie da się wstawić instrukcji
  pomiędzy — dziś złamanie tego to przesunięcie jednej linii.

run_compile / _compile_step → lustrzane do render. NIE unifikuj z render
  w tym PR (backlog ma 13 pozycji duplikacji — świadomie odłożonych).

OGRANICZENIE WOBEC FAZY 1: funkcje modułowe, które testy PODMIENIAJĄ, muszą
POZOSTAĆ funkcjami modułowymi. Faza 1 przecelowała ~15 miejsc podmiany na
moduł-właściciela; zwinięcie tych funkcji w metody _Clock/_Stage zerwałoby te
patche PO RAZ DRUGI. Jeśli któraś naprawdę powinna zostać metodą — przenieś
także patche, w tym samym PR i świadomie. Wypisz listę takich nazw ZANIM
zaczniesz projektować obiekty stanu.

UWAGA O TYPACH: repo NIE MA type-checkera (brak mypy/pyright w pyproject, CI
i pre-commit). Osobne typy dla filmu na osi nagrania i wirtualnej poprawiają
czytelność, ale SAME Z SIEBIE NICZEGO NIE WYMUSZAJĄ w czasie wykonania.
Realną ochroną niezmiennika jest test z fazy 0. Jeśli chcesz wymuszenia,
dodaj asercję isinstance — nie udawaj, że typy wystarczą.

NIEZMIENNIKI KOLEJNOŚCI — po refaktoringu mają być TRUDNIEJSZE do złamania,
nie łatwiejsze. Dla każdego napisz w PR, jak go chronisz:
  - cursor/slide/desktop PRZED chrome.js → jedna funkcja, której ciałem JEST
    kolejność, plus asercja runtime na liście zarejestrowanych
  - kompozycja popupu PRZED edycją czasu → osobne typy dla filmu na osi
    nagrania i wirtualnej; zamiana kolejności staje się błędem typu
  - monotoniczność last_freeze_frame → jedyny czytelnik i pisarz w _Clock
  - sonda nieobecności PRZED narracją → szew między "czy ten krok w ogóle
    się wydarzy" a "wykonaj go"

Po każdym commicie uruchom testy umiejscowienia (przeplatanie narracji,
dźwięk po zamrożeniu) — one jako jedyne łapią klasę błędów z pułapki wyżej.
````

---

## FAZA 4 — `recorder.py` (1328 → ~340)

````text
[PREAMBUŁA]

ZADANIE: recorder.py nie ma ANI JEDNEGO naruszenia complexity. Problem to
wyłącznie rozmiar: 23 z 37 metod klasy Recorder to napędzanie <select>
(72% linii klasy).

Zależność jest jednokierunkowa — żadna metoda rdzenia nie woła metody select.
Kod select czyta z self tylko: frame, page (jedno wywołanie), overlay (jeden
odczyt `is None`), open_hold_ms. To czysta ekstrakcja, nie węzeł.

DOCELOWO (zweryfikuj pomiarem):
  recorder/recorder.py        ~340   rdzeń + 2 delegatory + re-eksport
  recorder/select_driver.py   ~490   choreografia (jedyna część stanowa)
  recorder/select_errors.py   ~265   SelectDriveError, OPTION_MISSING, konstruktory
  recorder/select_probe.py    ~205   pytania do <select>, bezstanowe
  recorder/_js.py             ~190   10 stałych ze skryptami
  overlay/geometry.py         +8     center_of (zrywa cykl importów)

DWA CIĘCIA TO ZA MAŁO — przy samym _js.py + select_driver.py driver wychodzi
~900, a z jednym dodatkowym ~660. Potrzebne są cztery.

KSZTAŁT: wąskie zależności (page, frame, approach, animated, open_hold_ms),
NIE wsteczna referencja do Recorder — ta ostatnia daje god-class z jednym
przeskokiem i znów ukrywa prawdziwą zależność.
Przekaż `animated: bool`, nie `overlay`: driver pyta wyłącznie `is None`.

PUŁAPKA: `approach` przekaż jako LAMBDĘ, nie metodę związaną. Test podmienia
rec._approach na INSTANCJI, żeby próbkować geometrię listy po obu stronach
każdego przesunięcia kursora. Metoda związana zamrożona w konstruktorze
sprawi, że szpieg zobaczy zero wywołań.

KOLEJNOŚĆ COMMITÓW (każdy zielony, każdy czysto przenoszący):
  1. center_of → overlay/geometry.py (7 linii; zrywa przyszły cykl zanim powstanie)
  2. stałe JS → recorder/_js.py (najniższe ryzyko; przecelowuje 2 testy
     pilnujące, że definicja "czy ten select jest już wzbogacony" ma JEDNEGO
     właściciela — to jest test architektoniczny, nie kosmetyczny)
  3. błędy → select_errors.py (fasada pokrywa 7 miejsc importu; ZERO zmian w testach)
  4. probe + driver RAZEM (rozdzielenie przeniosłoby jedno wywołanie dwa razy)

SelectDriveError.reason: kontrakt "OPTION_MISSING = krok opcjonalny może być
pominięty, każdy inny powód MUSI zatrzymać przewodnik" pozostaje nietknięty.
Klasa przenosi się do select_errors.py, recorder.py ją re-eksportuje.

Uwaga na stałe czasowe OPTION_WAIT_MS i READY_WAIT_MS: mają po dwóch
czytelników (timeout ORAZ tekst komunikatu). Dziś jedna globalna trzyma je
zgodne przy podmianie. Po rozdzieleniu na moduły przekaż limit jawnie jako
parametr do konstruktora komunikatu — inaczej komunikat będzie cytował 5000 ms
po podmianie na 400.
````

---

## FAZA 5 — testy (14 plików > 600)

````text
[PREAMBUŁA]

ZADANIE: podziel pliki testowe przekraczające 600 linii, lustrzanie do podziału
źródeł (test_render_popup.py naprzeciw render/popup_*.py).

TWARDE OGRANICZENIE: tests/ nie ma ANI JEDNEGO conftest.py i to jest świadoma
decyzja — test_mux.py deklaruje ją w swoim docstringu ("no shared conftest by
design"). Zachowujemy tę własność. Pomocniki idą do jawnie importowanych
modułów `_<temat>_helpers.py` OBOK testów, które ich używają. Nic nie ma się
pojawiać w teście "znikąd".

TRZY PUŁAPKI MECHANICZNE:

1. Re-eksport fixture'a wymaga `# noqa: F401`.
   ruff ma select=["F"], a tests/** ignoruje tylko E501. `from ._x import page`
   wywali F401. To najbardziej prawdopodobna awaria całego podziału.

2. `pytestmark` NIE dziedziczy się przez import pomocnika.
   Każdy nowy plik musi przenieść swoje markery i skipif DOSŁOWNIE. Zgubiony
   skipif zmienia to, co CI uruchamia, i nikt tego nie zauważy — to
   najprawdopodobniejszy sposób na ciche zepsucie CI.
   Zalecenie: eksportuj blok markerów z modułu pomocniczego jako stałą
   (FFMPEG = [pytest.mark.ffmpeg, pytest.mark.skipif(...)]) i pisz
   `pytestmark = FFMPEG` — jedna definicja, nadal jawny import.
   Uwaga: repo ma dziś TRZY różne idiomy markerowania. Nie dodawaj czwartego.

3. Pliki, które po podziale przestają potrzebować ffmpeg — kuszące, ale to
   zmiana tego, co się uruchamia na maszynie bez ffmpeg. Przenieś marker
   dosłownie w commicie podziału, zdejmij go w OSOBNYM commicie.

test_selects_js.py (1933) — decyzja właściciela: dzielimy na 7 mimo
narracyjnej struktury. Docstring deklaruje, że kluczowy niezmiennik jest
"asserted first and hardest", a pytest zbiera alfabetycznie.
ŁAGODZENIE (zastosuj): plik z niezmiennikiem strukturalnym ZACHOWUJE nazwę
`test_selects_js.py`, pozostałe sześć dostaje sufiksy. Znak "." sortuje się
przed "_", więc kolejność "najważniejszy pierwszy" ocaleje.
Komentarze odsyłające do sąsiedniej sekcji ("unlike the geometry lookup above",
"the ceiling section 9 just established") przepisz na jawne odwołania do pliku
— po podziale są nieczytelne.

Trzymaj razem, mimo naturalnych szwów:
  - dwa bloki testów odporności na zawieszenie (drugi ma sens tylko obok pierwszego)
  - trzy poziomy łańcucha popup crop (to jedna opowieść "najpewniejsze najpierw")
````

---

## FAZA 6 — bramki (ostatnia dla Pythona)

````text
[PREAMBUŁA]

ZADANIE: zamknij limity maszynowo. Ta faza MUSI być ostatnia dla Pythona —
wcześniej blokowałaby własne PR-y.

1. pyproject.toml — DOPISZ "C901" do ISTNIEJĄCEJ listy select i DODAJ sekcję
   mccabe. ZOSTAW `ignore` oraz `per-file-ignores` DOKŁADNIE JAK SĄ.

     [tool.ruff.lint]
     select = ["E", "F", "I", "UP", "B", "C901"]   # <- dopisane C901
     ignore = ["UP040"]                             # <- BEZ ZMIAN
     # [tool.ruff.lint.per-file-ignores]            # <- BEZ ZMIAN

     [tool.ruff.lint.mccabe]                        # <- nowa sekcja
     max-complexity = 10

   Nie zastępuj całej sekcji powyższym fragmentem: usunięcie `ignore`
   i `per-file-ignores` daje 24 nowe naruszenia na niezwiązanych plikach
   (3x UP040, 17x E501 w reasoner.py, 4x B008 w cli.py) i wywalisz własną
   bramkę na kodzie, którego nie dotykałeś.
   Do per-file-ignores NIE WOLNO dopisać żadnego wpisu dla C901.

2. Ruff nie ma reguły na długość pliku. Napisz lokalny hook pre-commit
   (skrypt w scripts/), próg 600, zakres *.py, lista wyjątków pusta.
   Ten sam pomiar dodaj do CI, żeby nie dało się go ominąć przez --no-verify.

3. Utwórz CLAUDE.md (dziś nie istnieje). Zapisz w nim NIE same limity, ale
   powody, których nie widać z kodu:
     - dlaczego __init__.py pakietów jest fasadą
     - dlaczego fasada CELOWO nie re-eksportuje nazw podmienianych w testach
       (i że dodanie brakującego re-eksportu "dla wygody" jest regresją)
     - dlaczego szwy woła się przez obiekt modułu
     - dlaczego tests/ nie ma conftest.py
     - że `match` nie obniża CC w ruffie
   Bez tego następna sesja "uprości" fasadę i przywróci ciche zerwania.

4. Uruchom pełny suite łącznie z integracyjnymi i porównaj czas z baseline'em
   z fazy 0.
````

---

## FAZA 7 — `.js` (opcjonalna)

````text
[PREAMBUŁA]

ZADANIE: selects/selects.js (1427) i overlay/cursor.js (792) poniżej 600 linii.

Inna klasa ryzyka niż Python: kolejność rejestracji skryptów init jest
load-bearing (cursor/slide/desktop MUSZĄ być zarejestrowane przed chrome.js,
bo każdy z nich rozstrzyga swoją rolę czytając prawdziwe window.top, a chrome.js
przesłania `top`). Selects.js jest z tego kontraktu świadomie wyłączony —
przeczytaj komentarz na górze pliku, zanim cokolwiek ruszysz.

Podział wymaga sklejania po stronie Pythona. Wzorzec już istnieje:
selects/visibility.py ma shape_prelude(), który dokleja współdzieloną
definicję predykatu. Idź tą drogą, nie wymyślaj drugiego mechanizmu.

Hook z fazy 6 rozszerz o *.js DOPIERO po zakończeniu tej fazy.

Jeśli w trakcie okaże się, że podział pogarsza czytelność albo grozi
naruszeniem kontraktu ról — ZATRZYMAJ SIĘ i zgłoś. Ta faza jest opcjonalna
i wolno jej nie zrobić.
````
