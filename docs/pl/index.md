# Guidebot Recorder

Guidebot Recorder zamienia uporządkowany scenariusz przeglądarkowy zapisany w YAML
w powtarzalny film instruktażowy z animowanym kursorem i narracją TTS.

```text
*.scenario.yaml ── compile (AI) ──▶ *.compiled.yaml ── render (bez LLM) ──▶ *.mp4
```

!!! info "Jakiego agenta można użyć?"

    Standardowe polecenia **`guidebot compile` i `guidebot compile-set` używają
    wyłącznie Codex CLI**. Claude, Gemini, OpenCode i inne agenty mogą przygotować
    scenariusze oraz manifest zestawu, ale bez własnego adaptera `Reasoner` nie da się
    wybrać ich jako kompilatora.

    Agent nie odkrywa całej trasy. Autor zapisuje kolejność stron i operacji, a Codex
    rozwiązuje opis bieżącego elementu do strukturalnego targetu. Szczegóły opisuje
    strona [Agenci kompilujący](compiling-agents.md).

## Co robi Guidebot

1. Waliduje zamknięty schemat YAML.
2. Uruchamia scenariusz w świeżym kontekście Chromium z właściwym viewportem i
   `locale`.
3. Zamienia semantyczne opisy elementów na sprawdzone targety Playwrighta.
4. Zapisuje targety, ich tożsamość i zachowanie popupów w wersjonowanym
   `*.compiled.yaml`.
5. Odtwarza scenariusz bez LLM, nagrywa główne okno i obsługiwany popup, generuje TTS
   i składa wynik przez ffmpeg.

## Dwa sposoby publikowania języków

| Potrzeba | Pliki | Wynik |
|---|---|---|
| Ten sam interfejs i te same akcje, różna narracja | Jeden scenariusz z `audioTracks` i `translations` | Jeden MP4 z wieloma wybieralnymi ścieżkami audio |
| Różny język strony, host, ścieżka, etykiety albo przebieg | Pełny scenariusz na język oraz manifest render-set | Osobny jednościeżkowy MP4 na wariant |

Zobacz [Wiele ścieżek audio](multilingual-audio.md) i
[Zlokalizowane zestawy renderów](localized-render-sets.md).

## Od czego zacząć

<div class="grid cards" markdown>

-   :material-rocket-launch: **Pierwszy film**

    ---

    Instalacja, walidacja, kompilacja i render zwykłego scenariusza.

    [Szybki start](getting-started.md)

-   :material-robot: **Agenci i kompilatory**

    ---

    Granica odpowiedzialności Codexa i punkt rozszerzenia `Reasoner`.

    [Agenci kompilujący](compiling-agents.md)

-   :material-file-code: **Pliki scenariusza**

    ---

    Co tworzyć, generować, przeglądać i commitować.

    [Pliki scenariusza](scenario-files.md)

-   :material-book-open-variant: **Pełna składnia**

    ---

    Wszystkie pola konfiguracji, kroki i ograniczenia.

    [YAML scenariusza](scenario-reference.md)

</div>

## Obecny zakres

Guidebot jest oprogramowaniem beta. Standardowe CLI uruchamia Chromium, Codex jako
jedyny backend kompilacji i Edge TTS jako jedyny adapter narracji. Obsługiwany jest
jeden popup otwarty przez kliknięcie w całym scenariuszu; przełączenie do niego i
powrót po zamknięciu są automatyczne. Dodatkowe popupy, popupy otwierane poza akcją,
jawne przełączanie kart i zawartość iframe nie są obsługiwane.

Nie istnieje polecenie odkrywania trasy ani nagrywania ręcznej sesji, a `--auto-heal`
pozostaje niezaimplementowane. Przed automatyzacją produkcyjnego przebiegu przeczytaj
[Rozwiązywanie problemów](troubleshooting.md#obecne-ograniczenia).
