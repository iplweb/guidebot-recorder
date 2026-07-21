# Tworzenie przewodników PDF krok po kroku

Guidebot potrafi wyrenderować skompilowany scenariusz jako krajobrazowy przewodnik PDF — jeden
anotowany krok na stronę, obok tekstu narracji. Każda strona przewodnika zamraża kadr w momencie
zakończenia interaktywnego kroku i nakłada na niego adnotacje: ruchy kursora, cel kliknięcia
i wpisany tekst.

Ta funkcja nie wymaga LLM ani dodatkowych zależności poza skompilowanym sidecarem.

## Przegląd

Wygeneruj przewodnik PDF z już skompilowanego scenariusza:

```bash
uv run guidebot guide scenarios/login.scenario.yaml --out out/login-guide.pdf
```

Polecenie:

- Wczytuje źródłowy `login.scenario.yaml` i jego sidecar `login.compiled.yaml`;
- Otwiera świeży Chromium w skonfigurowanym viewporcie i locale;
- Przechodzi przez każdą akcję, przechwytując zrzuty ekranu i budując anotowane klatki;
- Eksportuje krajobrazowy PDF: zrzut ekranu po lewej, narracja po prawej.

Przewodnik wymaga wcześniejszego pomyślnego kroku `compile`. Nie produkuje wywołań LLM, syntezy
TTS ani wideo.

## Układ i typy stron

Jeden przewodnik PDF zawiera jedną lub więcej stron:

- **Interaktywny krok (click, hover, type)** — Anotowany zrzut ekranu (lewo), tekst narracji (prawo).
- **Nawigacja** — Strona z tekstem „Otwórz adres:" i adresem URL (kroki `navigate`).
- **Plansza podziału** — Karta w stylu slajdu wstawiona jako przerwa wizualna (kroki `slide`).
- **Bramy wait/when** — Bez strony. Warunkowe czekania i tło nie produkują wyjścia.

### Legenda adnotacji

Zrzuty ekranu są nakładane adnotacjami:

- **Strzałka** (linia zakrzywiona) — Ruch kursora z punktu A do punktu B.
- **Czerwone koło** — Cel kliknięcia myszy.
- **Czerwona ramka** — Tekst wpisany do pola (z `enterText` lub literalnym `teach` pisaniem).
- **Glow** (miękki halo) — Stan hover na elemencie.

## Tekst narracji: `say`, `teach` lub `caption`

Domyślnie strona PDF pokazuje narrację kroku — albo `say` dla samodzielnych kroków, albo `teach`
dla nauczonych akcji. Tekst jest wyświetlony bez syntezy TTS.

Aby nadpisać tekst PDF dla jednego kroku, użyj opcjonalnego pola `caption:`:

```yaml
steps:
  - teach: "Kliknij niebieski przycisk logowania, aby przejść dalej"
    caption: "Zaloguj się"
```

W tym przykładzie wyrenderowany PDF pokazuje „Zaloguj się" zamiast pełnego tekstu `teach`. Pole
`caption` jest ignorowane przez `render` i nie wpływa na skomponowane wideo. Gdy go pominiesz,
Guidebot wraca do `say` lub `teach` jak zwykle.

## Opcje wiersza poleceń

```bash
uv run guidebot guide SCENARIUSZ.yaml --out WYNIK.pdf [OPCJE]
```

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--out ŚCIEŻKA`, `-o ŚCIEŻKA` | wymagana | Docelowy `.pdf`. Katalogi-rodzice są tworzone. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pokazuje postęp budowania stron i szczegóły kroków. |

Wartość `--timeout` jest używana identycznie do poleceń `compile` i `render` i dotyczy
wszystkich akcji przeglądarki podczas generowania przewodnika.

## Obecne ograniczenia v1

Funkcja przewodnika ma następujący zakres:

- **Tylko jeden język** — Przewodniki używają kanonicznej narracji z `say` i `teach`; wielojęzyczne
  ścieżki audio i `translations` nie są obsługiwane. Aby stworzyć zlokalizowane przewodniki,
  skompiluj i zbuduj osobne pliki PDF dla każdego locale.
- **Brak scenariuszy z popupami** — Scenariusz otwierający popup jest odrzucany z wyraźnym błędem.
  Funkcja wykrywa to podczas fazy preflight i kończy się bez tworzenia wyjścia.
- **Brak grupowania wieloetapowego** — Kroki są renderowane indywidualnie. Przyszłe wersje mogą
  pozwolić na wizualne grupowanie sekwencji kroków lub numerowanie (np. „Krok 1 z 5").
- **Brak dostosowania layoutu PDF** — Marginesy, czcionki, kolory i wymiary strony są ustalone.

Jeśli Twój scenariusz wykracza poza te ograniczenia, użyj `render` do produkcji MP4.

## Walidacja skompilowanego sidecara

Przed uruchomieniem `guide` upewnij się, że scenariusz się kompiluje pomyślnie:

```bash
uv run guidebot validate scenarios/login.scenario.yaml
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
uv run guidebot guide scenarios/login.scenario.yaml --out out/guide.pdf --verbose
```

Jeśli scenariusz otwiera popup, polecenie `guide` będzie się nie powieść przy uruchomieniu
z wyraźnym błędem i kodem wyjścia 2. Warunkowe gałęzie `when:` są obsługiwane automatycznie —
brak elementu warunkującego oznacza, że cała gałąź jest pomijana i nie tworzy stron. Użyj
`compile --force` jeśli edytowałeś scenariusz, potem spróbuj ponownie `guide`.
