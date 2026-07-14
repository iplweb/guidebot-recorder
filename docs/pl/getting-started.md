# Szybki start

Poniższa procedura tworzy, kompiluje i renderuje jeden scenariusz. Polecenia uruchamiaj
z katalogu głównego repozytorium.

## 1. Zainstaluj wymagane narzędzia

Potrzebujesz Pythona 3.12+, [uv](https://docs.astral.sh/uv/), Chromium dla
Playwrighta, ffmpeg z ffprobe oraz Codex CLI do fazy kompilacji.

```bash
uv sync
uv run playwright install chromium
```

=== "macOS"

    ```bash
    brew install ffmpeg
    ```

=== "Debian / Ubuntu"

    ```bash
    sudo apt update
    sudo apt install ffmpeg
    ```

Zainstaluj i uwierzytelnij Codex CLI:

```bash
npm install -g @openai/codex
codex login
codex login status
```

Guidebot używa sesji zapisanej przez Codex CLI. Może to być logowanie kontem ChatGPT
albo kluczem API.

## 2. Utwórz źródło

Zalecana nazwa to `scenarios/login.scenario.yaml`:

```yaml
config:
  title: "Logowanie"
  baseUrl: https://staging.example.com
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts:
    provider: edge
    voice: pl-PL-ZofiaNeural
    lang: pl-PL
    trackLanguage: pol
    title: Polski

steps:
  - navigate: /login
  - say: "Pokażę, jak zalogować się do systemu."
  - enterText:
      into: "pole adresu e-mail"
      text: "${DEMO_EMAIL}"
    say: "Wpisuję adres e-mail."
  - enterText:
      into: "pole hasła"
      text: "${DEMO_PASSWORD}"
    say: "Wpisuję hasło."
  - teach: "Kliknij przycisk Zaloguj"
```

W jednym kroku umieszczaj najwyżej jedną główną komendę. `say` może występować
samodzielnie albo towarzyszyć akcji. Pełną składnię opisuje
[YAML scenariusza](scenario-reference.md).

Opcjonalny syntetyczny pasek przeglądarki włącza konfiguracja renderowa:

```yaml
config:
  chrome:
    enabled: true
    showUrl: true
    typeOnNavigate: true
```

## 3. Przekaż wartości przez środowisko

Guidebot nie wczytuje `.env` automatycznie:

```bash
export DEMO_EMAIL=guidebot@example.com
export DEMO_PASSWORD='wartosc-testowa'
```

`${NAZWA}` jest rozwijane wyłącznie w tekstowym `navigate`, `navigate.url` oraz
`enterText.text`. Brak zmiennej powoduje błąd również podczas walidacji. Wartość nie
trafia do źródła ani sidecara, ale może być widoczna w filmie, logach aplikacji i
materiałach roboczych.

## 4. Zwaliduj schemat

```bash
uv run guidebot validate scenarios/login.scenario.yaml
```

Sukces wypisuje `OK`. Walidacja wykrywa brakujące i nieznane pola, błędny kształt
kroków, niepełne tłumaczenia oraz brak zmiennych. Nie otwiera strony i nie potwierdza,
że elementy istnieją.

## 5. Skompiluj targety

Kompilacja naprawdę wykonuje scenariusz, dlatego używaj resetowalnego środowiska
testowego i jednorazowego konta:

```bash
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
```

Guidebot tworzy świeży kontekst z `viewport` i `locale`, przechodzi cały przebieg i
zapisuje `scenarios/login.compiled.yaml`. Sidecar w wersji kompilatora 2 zawiera jeden
slot na każdy krok, fingerprint instrukcji oraz zamrożoną tożsamość celu.

Jeśli kliknięcie otworzy popup, Guidebot zapisuje ten fakt, automatycznie przenosi
dalsze kroki do popupu i wraca do strony głównej po jego zamknięciu. W całym
scenariuszu obsługiwany jest najwyżej jeden popup.

Przy błędzie pozostaw okno do inspekcji:

```bash
uv run guidebot compile scenarios/login.scenario.yaml \
  --headed --pause-on-error -v
```

Nie renderuj po nieudanej kompilacji. Sidecar jest zapisywany po kolejnych krokach,
więc może zawierać tylko częściowy postęp; doprowadź `compile` do sukcesu.

## 6. Przejrzyj i commituj YAML

```text
scenarios/
├── login.scenario.yaml   # źródło autora
└── login.compiled.yaml   # wygenerowany sidecar v2
```

Commituj oba pliki, ale sidecara nie edytuj ręcznie. Sprawdź w diffie zmianę targetów,
`opens_popup` i ewentualny literalny `input_text` po akcji `teach` → `type`.

## 7. Wyrenderuj film

```bash
uv run guidebot render scenarios/login.scenario.yaml \
  --out out/login.mp4 --headed -v
```

Przed TTS i uruchomieniem przeglądarki render sprawdza nazwę źródła, wersję
kompilatora, liczbę slotów i fingerprinty akcji. Następnie:

- generuje brakujące segmenty w `.guidebot/audio/`;
- tworzy świeży kontekst Chromium z tym samym `viewport` i `locale`;
- sprawdza na żywo tożsamość zwykłych akcji;
- nagrywa główne okno oraz obsługiwany popup;
- zapisuje materiały pod `out/.guidebot_video/login/`;
- publikuje atomowo `out/login.mp4` i pełną ścieżkę WAV narracji.

Render nie wywołuje LLM. Może wymagać sieci do aplikacji oraz Edge TTS. Standardowe
CLI odrzuca każdy provider inny niż `edge` przed uruchomieniem nagrania.

## 8. Wybierz model językowy publikacji

Jeżeli obraz i akcje są wspólne, dodaj alternatywne narracje do jednego scenariusza:

[Wiele ścieżek audio](multilingual-audio.md)

Jeżeli język strony, host, ścieżki lub opisy akcji różnią się, utwórz pełny scenariusz
na język i manifest:

[Zlokalizowane zestawy renderów](localized-render-sets.md)

## 9. Kiedy ponownie kompilować

| Zmiana | Co zrobić |
|---|---|
| `say`, `translations`, `voice`, `title`, `trackLanguage`, `audioTracks`, `cursor`, `chrome` | Wystarczy ponowny render. |
| Instrukcja targetu, rodzaj akcji, `viewport`, `locale`, domyślne `tts.lang` | Uruchom `compile`; preflight odrzuci stary sidecar. |
| Sama aplikacja, DOM, trasa ustawiona w `navigate` albo stan konta | Użyj `compile --force`, ponieważ szybkie sprawdzenie nie otwiera strony. |
| Aktualizacja ze sidecara v1 | Skompiluj ponownie; v1 jest celowo uznawane za nieaktualne. |

```bash
uv run guidebot compile scenarios/login.scenario.yaml --force --headed -v
```
