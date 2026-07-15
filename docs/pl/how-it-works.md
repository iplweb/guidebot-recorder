# Jak to działa

Guidebot jest kompilatorem dwufazowym. Wersjonowany sidecar oddziela niedeterministyczne
rozwiązanie targetów od renderu bez LLM.

```text
                         COMPILE
źródło YAML ─▶ świeży Chromium ─▶ snapshot ─▶ Codex ─▶ walidacja targetu
      │                                                   │
      └────────────── *.compiled.yaml v2 ◀────────────────┘

                          RENDER
źródło + sidecar ─▶ mocny preflight ─▶ świeży Chromium ─▶ MP4 + TTS
                                                   bez LLM
```

## Walidacja

`guidebot validate` rozwija dozwolone zmienne środowiskowe i sprawdza zamknięty
schemat Pydantic. Nie otwiera przeglądarki ani strony. Potwierdza kształt dokumentu,
nie istnienie targetów.

Manifest render-set nie ma osobnego `validate-set`; `compile-set` i `render-set`
walidują cały manifest oraz wszystkie źródła przed uruchomieniem przeglądarki.

## Kompilacja

`guidebot compile` tworzy świeży kontekst Chromium z `config.viewport` i
`config.locale`, a następnie wykonuje scenariusz od kroku zero. Dla kroku z targetem:

1. sprawdza, czy zapis v2 pasuje do rodzaju komendy, instrukcji, konfiguracji i stanu;
2. jeśli compile już otworzył stronę, weryfikuje też target i jego tożsamość na żywo;
3. przy braku aktualnego zapisu zbiera do 200 kandydatów i pyta reasoner;
4. buduje locator wyłącznie ze strukturalnych danych, wymaga jednego zgodnego elementu
   i zamraża jego niezależną tożsamość;
5. wykonuje akcję, wykrywa ewentualny popup i atomowo zapisuje postęp sidecara.

`say`, `navigate` i liczbowy `wait` nie wywołują AI. Kompilacja ma realne skutki:
nawiguje, klika, wykonuje hover i wpisuje wartości.

## Sidecar v2

Dla `login.scenario.yaml` powstaje `login.compiled.yaml` zawierający:

- `compiler_version: 2` i nazwę źródła;
- jeden slot akcji na każdy krok (`null` dla kroku bez targetu);
- strukturalny target i zamrożoną tożsamość;
- fingerprint rodzaju komendy, instrukcji, konfiguracji, oczekiwania i stanu wait;
- `opens_popup`, gdy kliknięcie otworzyło obsługiwane okno;
- `input_text`, gdy `teach` bezpiecznie rozpoznał wpisanie jawnego literału.

Stare pliki bez wersji są traktowane jako v1 i wymagają ponownej kompilacji. Sidecar
commituj i przeglądaj, ale nie edytuj ręcznie.

## Szybkie sprawdzenie i ponowne użycie

Przed otwarciem Chromium `compile` sprawdza nazwę źródła, wersję, liczbę i wyrównanie
slotów oraz pełne fingerprinty targetów. Gdy wszystko jest aktualne, kończy bez
przeglądarki. Zmiana samej narracji lub alternatywnych tłumaczeń nie wymaga compile.

Ta szybka ścieżka nie widzi żywego DOM ani wpływu zmienionego `navigate`, danych konta,
cookies czy odpowiedzi serwera. Po zmianie aplikacji lub przygotowania trasy użyj:

```bash
uv run guidebot compile scenarios/flow.scenario.yaml --force
```

Jeżeli przeglądarka zostanie uruchomiona, zapisany target jest dodatkowo sprawdzany na
żywo przed ponownym użyciem. `--force` pomija cache targetów i rozwiązuje je od nowa.

## Popup

Popup musi zostać otwarty przez akcję `click`. Guidebot przypisuje nowe okno do
konkretnego kliknięcia, ustawia ten sam viewport, przełącza dalsze kroki na popup i po
jego zamknięciu wraca do strony głównej. W sidecarze zapisuje `opens_popup: true`.

Render nagrywa osobne obrazy stron i składa oś `main → popup → main`. Natywny pasek
kart Chromium nie należy do nagrania; syntetyczny `chrome` i kursor są wstrzykiwane do
obu stron. Popup pozostawiony otwarty jest widoczny do końca filmu.

Kontrakt v1 popupów pozwala na najwyżej jeden popup w całym scenariuszu. Drugie,
wielokrotne, zbyt późne lub otwarte poza kliknięciem okno, asynchroniczne zamknięcie
oraz zamknięcie strony głównej powodują błąd. Nie istnieje jawna komenda przełączania
kart. Zawartość iframe nadal nie jest obsługiwana.

## Mocny preflight renderu

Zanim render uruchomi TTS i przeglądarkę, sprawdza:

- nazwę źródła i `compiler_version`;
- liczbę slotów;
- rodzaj akcji, instrukcję, config hash, stan i oczekiwanie każdego fingerprintu;
- obecność `input_text` dla `teach` → `type` i poprawność metadanych popupu.

Podczas odtwarzania zwykłe akcje dodatkowo sprawdzają żywy target i tożsamość.
Warunkowy `waitFor` nie porównuje zamrożonej tożsamości; `hidden` może celowo nie mieć
jej wcale.

## Render i audio

Render najpierw syntetyzuje wszystkie narracje, potem nagrywa jeden wspólny obraz.
Dla wielu języków akcja czeka na najdłuższą narrację danego kroku. Po nagraniu
Guidebot buduje pełnej długości WAV dla każdej ścieżki i atomowo publikuje MP4.

```text
ten sam UI ─▶ jeden scenariusz + audioTracks ─▶ jeden MP4, wiele audio
różny UI    ─▶ manifest + pełne scenariusze  ─▶ osobny MP4 na język
```

Pierwszy wariant opisuje [Wiele ścieżek audio](multilingual-audio.md), a drugi
[Zlokalizowane zestawy renderów](localized-render-sets.md).

## Granica powtarzalności

Brak LLM podczas renderu nie gwarantuje identycznych pikseli. Wynik nadal zależy od
stanu aplikacji, sieci, konta, fontów, Chromium, dostępności TTS i danych. Traktuj
scenariusz jak test end-to-end: pinuj środowisko i resetuj stan przed każdym przebiegiem.
