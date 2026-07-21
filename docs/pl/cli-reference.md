# Dokumentacja CLI

```bash
uv run guidebot --help
```

Publiczne polecenia to `validate`, `compile`, `render`, `guide`, `compile-set` i `render-set`.

## `guidebot validate`

```bash
uv run guidebot validate ŚCIEŻKA
```

Wczytuje jeden scenariusz, rozwija dozwolone ENV i sprawdza zamknięty schemat bez
Chromium, agenta i targetowej strony. Sukces wypisuje `OK`. Nie obsługuje manifestu
render-set; polecenia zestawu wykonują własny pełny preflight.

## `guidebot compile`

```bash
uv run guidebot compile ŚCIEŻKA [OPCJE]
```

Wykonuje scenariusz od początku w świeżym kontekście z jego `viewport` i `locale`, a
następnie zapisuje sąsiedni sidecar v2. Standardowe polecenie używa `CodexReasoner`.

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--headed` | wyłączone | Pokazuje Chromium. |
| `--force` | wyłączone | Pomija cache targetów i rozwiązuje je od nowa. |
| `--pause-on-error` | wyłączone | Po błędzie zatrzymuje widoczną stronę do inspekcji. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp, kroki i reuse. |

Najpierw wykonywane jest szybkie sprawdzenie nazwy źródła, compiler v2, liczby slotów
i fingerprintów. Gdy sidecar jest aktualny, Chromium nie startuje. To sprawdzenie nie
widzi zmian samej aplikacji, stanu konta ani wpływu zmienionego `navigate`; wtedy użyj
`--force`.

`--timeout` nie steruje Codexem. Każda próba `codex exec` ma osobny limit 60 sekund, a
ponowienia mogą wydłużyć czas jednego targetu. Scenariusz z `steps: []` nie zapisuje
sidecara i nie może być renderowany przez standardowe CLI.

## `guidebot compile-set`

```bash
uv run guidebot compile-set MANIFEST [OPCJE]
```

Waliduje manifest i wszystkie pełne scenariusze, a potem kompiluje warianty po kolei
w kolejności manifestu. Każdy dostaje świeży kontekst i własny sidecar obok źródła.
Aktualne warianty są ponownie używane; gdy wszystkie są aktualne, przeglądarka nie
startuje.

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--headed` | wyłączone | Pokazuje Chromium. |
| `--force` | wyłączone | Kompiluje od nowa wszystkie warianty. |
| `--pause-on-error` | wyłączone | Zatrzymuje stronę wariantu po błędzie. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp. |

Polecenie używa tego samego CodexReasoner co zwykły `compile`. Kończy się na pierwszym
błędzie; wcześniejsze sidecary pozostają, późniejsze warianty nie startują.

## `guidebot render`

```bash
uv run guidebot render ŚCIEŻKA --out WYNIK.mp4 [OPCJE]
```

Przed TTS i Chromium sprawdza nazwę źródła, wersję, liczbę slotów oraz fingerprinty.
Następnie generuje wszystkie narracje, nagrywa świeży kontekst i publikuje MP4 bez LLM.

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--out ŚCIEŻKA`, `-o ŚCIEŻKA` | wymagana | Docelowy `.mp4`. |
| `--headed` | wyłączone | Pokazuje Chromium podczas nagrania. |
| `--pause-on-error` | wyłączone | Zatrzymuje stronę po błędzie. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp TTS i kroków. |
| `--auto-heal` | wyłączone | Zarezerwowane; włączenie kończy się błędem. |
| `--hold-frame` / `--no-hold-frame` | nieustawione — bierze `config.holdFrameForNarration` | Nadpisuje `holdFrameForNarration` scenariusza tylko na ten przebieg. `--no-hold-frame` nagrywa narrację każdego kroku w czasie rzeczywistym, tak jak zanim ta funkcja powstała — użyj go, gdy animacje scenariusza muszą działać przez cały czas lektora. Żadna z flag nie zmienia pliku konfiguracji. |
| `--hold-frame-settle FLOAT` | nieustawione — bierze `config.holdFrameSettle` | Nadpisuje `holdFrameSettle` tylko na ten przebieg, w sekundach. Obowiązuje to samo minimum co dla pola konfiguracji (dwie klatki, `2/25` s). |
| `--dump-timeline` | wyłączone | Zapisuje obok wideo wyliczoną oś czasu zamrożeń jako `<nazwa>.timeline.json`. Przydatne, gdy audio i wideo w wyrenderowanym pliku wydają się rozjeżdżać — pozwala sprawdzić, gdzie i na jak długo obraz był zamrożony. |

Standardowe CLI wymaga, aby provider każdej skonfigurowanej ścieżki był `edge`.
Mieszane albo inne nazwy są odrzucane przed nagraniem. API Pythona może użyć innego
adaptera, ale jeden render nadal wymaga wspólnej nazwy providera dla wszystkich audio.

`audioTracks` tworzy wiele strumieni w jednym MP4. Popup zapisany przez compile jest
automatycznie nagrywany i składany z główną stroną.

Zamrożenie klatki na czas narracji skraca nagrywanie z grubsza o całkowitą długość
lektora, nie zmieniając długości ani tempa gotowego filmu — zmienia się tylko czas
nagrywania i, przy domyślnym ustawieniu, wygląd pod narracją (strona stoi
nieruchomo, zamiast się animować). Pełne wyjaśnienie:
[`holdFrameForNarration` i `holdFrameSettle`](scenario-reference.md#holdframefornarration-i-holdframesettle)
w dokumentacji YAML scenariusza.

## `guidebot guide`

```bash
uv run guidebot guide ŚCIEŻKA --out WYNIK.pdf [OPCJE]
```

Wczytuje źródło i sąsiedni sidecar, następnie buduje krajobrazowy przewodnik PDF z jednym
anotowanym zrzutem ekranu na znaczący krok, tekstem narracji obok i legendą wizualną
(strzałkami, kołami, ramkami, glow).

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--out ŚCIEŻKA`, `-o ŚCIEŻKA` | wymagana | Docelowy `.pdf`. Katalogi-rodzice są tworzone. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp budowania stron i szczegóły kroków. |

To polecenie nie wykonuje żadnych wywołań LLM. Każda strona przewodnika przechwytuje kadr
w momencie zakończenia interaktywnego kroku (`click`, `hover`, `enterText`, `teach`).
Kroki `navigate` tworzą jedną stronę zawierającą tylko tekst. Kroki `slide` wstawiają
wizualny podział sekcji. Bramy `wait` i `when` nie produkują wyjścia; brak elementu
warunkującego powoduje, że całą gałąź jest pomijana.

Użyj `caption:` na kroku, aby nadpisać tekst PDF (wraca do `say` lub `teach` gdy
pominięty). Pełne wyjaśnienie, ograniczenia (jeden język, brak popupów, brak grupowania)
i legenda adnotacji znajdują się w [Tworzeniu przewodników PDF krok po kroku](pdf-guide.md).

## `guidebot render-set`

```bash
uv run guidebot render-set MANIFEST \
  --output-dir KATALOG [OPCJE]
```

`--output-dir` ma alias `--out-dir` i jest wymagane. Ścieżki `output` z manifestu są
rozwiązywane pod tym katalogiem. Polecenie tworzy osobny, jednościeżkowy MP4 dla
każdego pełnego scenariusza.

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--output-dir`, `--out-dir` | wymagana | Korzeń wszystkich outputów. |
| `--headed` | wyłączone | Pokazuje Chromium. |
| `--pause-on-error` | wyłączone | Zatrzymuje stronę wariantu po błędzie. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp. |

Przed uruchomieniem przeglądarki sprawdzane są wszystkie ścieżki, kolizje, provider
`edge` i aktualność sidecarów. Brak lub stary sidecar powoduje instrukcję uruchomienia
`compile-set`; render nigdy nie wywołuje reasonera automatycznie.

Warianty renderują się sekwencyjnie. Błąd zachowuje wcześniejsze gotowe outputy i
poprzedni poprawny plik wadliwego wariantu; późniejsze warianty nie startują.

## Zmienne środowiskowe

Guidebot czyta środowisko procesu i nie ładuje `.env`:

```bash
DEMO_EMAIL=user@example.com \
  uv run guidebot compile scenarios/login.scenario.yaml
```

Używaj tych samych wartości podczas compile i render, jeżeli wpływają na przebieg.
Manifest nie rozwija ENV, ale jego scenariusze robią to w dozwolonych polach.

## Dokumentacja

```bash
uv sync --group docs
uv run --group docs mkdocs serve
uv run --group docs mkdocs build --strict
```
