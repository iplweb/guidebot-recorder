# Guidebot-recorder — design (spec)

Data: 2026-07-14
Status: zaakceptowany do napisania planu implementacji

## 1. Cel i zakres

Narzędzie, które z tekstowego opisu scenariusza (YAML) generuje **film
szkoleniowy**: bot wchodzi na stronę, przechodzi daną funkcję krok po kroku
(Playwright), pokazuje kursor i kliknięcia, a lektor (TTS) tłumaczy, co się
dzieje. Produktem wyjściowym jest **plik `.mp4` z narracją głosową**.

Rdzeń pomysłu to **kompilator**: scenariusz pisany intencjami po ludzku
(„kliknij Zaloguj") zostaje **skompilowany** przez AI do postaci z zamrożonymi,
konkretnymi namiarami na elementy. Dzięki temu właściwe renderowanie filmu jest
w pełni **deterministyczne** i nie wymaga LLM-a: przeglądarka przechodzi całą
funkcję od początku do końca jednym ciągiem, na świeżej sesji, bez doczepiania
się w trakcie.

### Zakres v1
- Faza `compile` (intencje → wkompilowane akcje) z resolverem AI.
- Faza `render` (deterministyczny film `.mp4` z lektorem).

### Zaprojektowane, ale odłożone (v-next)
- Faza `record` — nagrywanie własnych kliknięć użytkownika wprost do scenariusza
  (bez AI). Format kroku i model danych projektujemy tak, by `record` wpiął się
  później bez przeróbek.

## 2. Model kompilatora — dwie fazy

```
[intencja YAML]   --compile (AI/Codex)-->  ┐
[record (v-next)] --przechwyć-->            ├─> [scenario.yaml z akcjami] --render--> [film .mp4]
[ręczna edycja]   -------------------------┘        (0×LLM, świeża przeglądarka, 1 przejście)
```

### Faza `compile` (`guidebot compile scenario.yaml`)
- Uruchamia scenariusz i dla każdego kroku wymagającego namiaru woła
  **ElementResolver** (LLM). Wynik — semantyczny locator + typ akcji — zostaje
  **wpisany w ten sam plik** pod danym krokiem jako `cachedAction`.
- To **jedyna** faza, w której działa AI.
- Edycja pliku jest **w miejscu** (round-trip, zob. §4): zachowuje formatowanie,
  kolejność i komentarze; dokłada wyłącznie klucz `cachedAction`.

### Faza `render` (`guidebot render scenario.yaml`)
- **Zero LLM.** Czyta wkompilowane `cachedAction`, odtwarza kroki czystym
  Playwrightem — te same kliknięcia, ten sam timing, powtarzalnie.
- Świeża przeglądarka, całość jednym przejściem, brak doczepiania się do
  istniejącej sesji.
- Krok wymagający namiaru bez `cachedAction` → **twardy błąd** „uruchom
  `compile`". Istnieje flaga `--auto-heal` (dopuszcza LLM w renderze), domyślnie
  **wyłączona**, bo psuje determinizm.

**Niezmiennik:** cała „inteligencja" jest wypychana do fazy `compile` i zamrażana
w pliku; render pozostaje deterministyczny.

## 3. Format scenariusza (YAML deklaratywny)

Scenariusz to lista kroków. YAML jest **formatem autorskim**; pod spodem jest
wspólne Python API (`Recorder`, §6), a runner YAML-a to tylko jeden frontend nad
nim. Ten sam `Recorder` jest dostępny bezpośrednio z Pythona dla przypadków
wymagających logiki (pętle itp.) — bez wstawek kodu w YAML-u.

### Komendy

| Komenda | Znaczenie | Akcja | Namiar (cache) |
|---|---|---|---|
| `say` | Czysta narracja, nic nie robi | — | nie |
| `teach` | Lektor mówi całe zdanie-przewodnik; LLM wyłuskuje z niego akcję i ją wykonuje | tak (wnioskowana) | tak |
| `enterText` | Wpisanie tekstu w pole (jawna wartość) | type | tak (na `into`) |
| `navigate` | Przejście pod URL | goto | nie |
| `wait` | Oczekiwanie (czas / warunek) | — | nie |
| `click` / `hover` | Jawny escape-hatch (akcja bez narracji lub gdy narracja ≠ akcja) | click/hover | tak |

`enterText` ma opcjonalny `say` (własna, inna narracja). `click`/`hover` również
mogą mieć opcjonalny `say`.

### `teach` — workhorse

Wartość `teach` to **całe zdanie w stylu przewodnika** („Aby się zalogować,
należy kliknąć przycisk Zaloguj w prawym górnym rogu"). Lektor wypowiada je w
całości, a kompilator:
1. **wyłuskuje część wykonawczą** ze zdania („kliknij Zaloguj w prawym górnym
   rogu"),
2. **wnioskuje typ akcji** (click / hover / …),
3. **rozwiązuje cel** do semantycznego locatora,
4. zapisuje wszystko w `cachedAction` (render niczego już nie domyśla).

`teach` obsługuje kliki / hovery / akcje wskazujące. **Wpisywania** się przez
`teach` nie robi — wartości do wpisania nie da się sensownie wyciągnąć ze zdania
(„wpisz swój email" — jaki?), więc typowanie ma jawny `enterText.text`.

### Przykład — przed i po `compile` (ten sam plik)

Przed:
```yaml
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: https://app.example.com
  - teach: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu"
  - enterText: { into: "pole email", text: "user@x.pl" }
    say: "Teraz wpisuję swój adres e-mail."
```

Po `guidebot compile` (dokłada tylko `cachedAction`):
```yaml
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: https://app.example.com
  - teach: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu"
    cachedAction:
      action: click
      role: button
      name: "Zaloguj"
      locator: "get_by_role('button', name='Zaloguj')"
      compiledFrom: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu"
  - enterText: { into: "pole email", text: "user@x.pl" }
    say: "Teraz wpisuję swój adres e-mail."
    cachedAction:
      action: type
      role: textbox
      name: "Email"
      locator: "get_by_role('textbox', name='Email')"
      compiledFrom: "pole email"
```

## 4. Kompilacja in-place (jeden plik)

- **Jeden plik** — brak osobnego artefaktu „compiled". Kompilator wstrzykuje
  `cachedAction` do tego samego `scenario.yaml`.
- **Round-trip** przez `ruamel.yaml`: zachowanie formatowania, kolejności i
  komentarzy; dokładany jest wyłącznie klucz `cachedAction`.
- **Idempotencja:** `compile` woła LLM tylko dla kroków **bez** `cachedAction`;
  skompilowane pomija. `--force` przelicza wszystko.
- **Wykrywanie nieaktualności (drift):** `cachedAction.compiledFrom` pamięta
  instrukcję źródłową. Jeśli tekst kroku (`teach`/`click`/`enterText.into`)
  różni się od `compiledFrom` → krok jest re-resolvowany, reszta nietknięta.
- **Git:** cały scenariusz (z akcjami) commitowany; `git diff` pokazuje, gdy
  strona się zmieniła i namiar uległ zmianie.

### Schemat `cachedAction`
- `action`: `click | hover | type | goto` — zamrożony typ akcji.
- `role`, `name`: semantyczny cel (drzewo dostępności).
- `locator`: gotowe wyrażenie Playwrighta (preferowane `get_by_role(...)`).
- `compiledFrom`: instrukcja źródłowa (do wykrywania driftu).

## 5. Resolver (tylko w `compile`, wołany rzadko)

Rozbity na dwie części za jednym interfejsem:

### 5.1 PageContext (wspólne, nasze)
Playwright wyciąga **accessibility-snapshot** aktualnej strony. To wejście dla
Reasonera; to również powód, dla którego cache'ujemy **semantyczne** locatory
(`get_by_role` po dostępnej nazwie), znacznie odporniejsze na zmiany layoutu niż
kruchy CSS.

### 5.2 Reasoner (wymienny backend)
Mapuje `(snapshot, instrukcja) → {action, role, name}`. Wybierany w configu.

- **Default: `codex exec`** — korzysta z subskrypcji (płaska opłata), zero kosztu
  API. To świadoma decyzja: resolver odpala się rzadko i jest tolerancyjny na
  latencję, więc CLI na subskrypcji jest OK.
- Alternatywy: `claude -p` (subskrypcja Max), `opencode`, oraz backend
  **Claude Messages API** (klucz API, per-token) — do CI / determinizmu /
  środowisk bez zainstalowanego CLI.

### 5.3 Mechanizmy dostarczenia kontekstu
- **A. Snapshot → agent (domyślny).** *My* robimy accessibility-snapshot i
  wysyłamy go jako **tekst** do agenta („oto drzewo dostępności; który element to
  «...»? zwróć action+role+name w JSON; **nie klikaj**"). Agent nie dotyka
  przeglądarki: zero konfiguracji MCP, zero portów, zero wyścigu o stronę,
  dowolny provider.
- **B. CDP-attach (zaawansowany).** Chrome wystawiony na porcie
  (`--remote-debugging-port`); agent z browser-MCP podpina się i **interaktywnie**
  bada stronę. Dla stron mocno dynamicznych / canvas, gdzie statyczny snapshot
  nie wystarcza. Więcej ruchomych części.

### 5.4 Trust-but-verify
Cokolwiek Reasoner zwróci, **walidujemy na żywej stronie**: locator musi trafiać
w **dokładnie 1 element**. Jeśli 0 lub >1 → **re-prompt** z feedbackiem (lista
kandydatów). Dopiero zwalidowany namiar trafia do `cachedAction`. To bramka
jakości, która niweluje luźniejszy (nie-schema) output CLI.

### 5.5 Rola LLM — granica
LLM/agent działa **wyłącznie w `compile`** (pierwsze przejście / drift).
**Nigdy** nie steruje przeglądarką „na żywo" przy renderze — to zabiłoby
determinizm i odtwarzalność filmu.

## 6. Silnik `Recorder` (Python API) i frontendy

- **`Recorder`** — jedyne miejsce, które „wie jak": `navigate / say / teach /
  enter_text / click / hover / wait`. Sedno, wspólne dla YAML i skryptów.
- **YAML runner** — iteruje kroki scenariusza i woła `Recorder`.
- **Skrypt Python** (furtka mocy) — pisze wprost przeciw `Recorder`, z pełnym IDE
  i importami, gdy potrzeba logiki. Bez wstawek kodu w YAML (hybryda odłożona,
  YAGNI).

## 7. Wizualizacja kursora i kliknięć — overlay w DOM

Playwright steruje programowo i **nie renderuje** kursora ani kliknięć na wideo.
Dlatego wstrzykujemy do strony **sztuczny kursor** (element HTML/SVG) oraz
animacje:
- płynny ruch wskaźnika do celu,
- „ripple" przy kliknięciu,
- podświetlenie (highlight) elementu docelowego.

Kursor to nasz element (nie systemowy) — deterministyczny, działa headless.
Nagrywa wbudowane wideo Playwrighta (`context.record_video`).

## 8. Narracja (TTS) i montaż audio

- **Model czasu: narracja steruje tempem.** Przy kroku z narracją bot
  **najpierw mówi** (odtwarza TTS do końca zdania), **potem** wykonuje akcję.
  Timing wynika z długości audio — zawsze zsynchronizowane, prosto.
- **TTS za interfejsem** — provider wymienny (np. ElevenLabs / OpenAI / edge-tts).
- **Montaż:** render zbiera segmenty audio na osi czasu wideo; na końcu **ffmpeg**
  miksuje je z nagraniem Playwrighta do finalnego `.mp4`.

## 9. Przepływ pojedynczego kroku `teach` (render)

```
krok: teach: "Aby się zalogować, należy kliknąć Zaloguj w prawym górnym rogu"
       cachedAction: {action: click, locator: "get_by_role('button', name='Zaloguj')"}

RENDER:
1. TTS.render(zdanie) → audio, znany czas T
2. overlay: (opcjonalny napis/dymek), start audio
3. czekaj aż lektor skończy (T)              ← narracja steruje tempem
4. cachedAction.locator → locator Playwrighta
5. overlay: płynny ruch kursora do elementu + ripple + highlight
6. Playwright wykonuje cachedAction.action (tu: click)
7. zapisz segment audio na osi czasu wideo
```

## 10. Artefakty i układ projektu

```
moje-szkolenie/
  login.scenario.yaml      # source + wkompilowane akcje (jeden plik, w git)
  out/login.mp4            # generowane przez `render`

guidebot_recorder/         # pakiet aplikacji (uv + pyproject)
  scenario/    # schema + loader + round-trip (ruamel)
  recorder/    # Recorder (Python API) + YAML runner
  resolver/    # ElementResolver: PageContext + Reasoner (codex/claude/api) + walidacja
  overlay/     # injektowany JS: sztuczny kursor + animacje
  tts/         # interfejs TTS + providerzy
  video/       # nagrywanie + mux (ffmpeg)
  cli.py       # compile / render / validate
```

## 11. Obsługa błędów (fail-loud, nigdy po cichu)

- **compile — element niejednoznaczny** (locator trafia w 0 lub >1): błąd + lista
  kandydatów; autor doprecyzowuje instrukcję. (Wewnętrznie: re-prompt resolvera.)
- **render — brak `cachedAction`** przy kroku wymagającym namiaru: twardy błąd
  „uruchom `compile`".
- **render — locator już nie trafia**: twardy błąd „re-compile" (chyba że
  `--auto-heal`, domyślnie off).
- **TTS padło**: błąd — nie cichy film bez głosu.
- **Nawigacja / timeout Playwrighta**: propagujemy, nie łykamy.

## 12. Testy

- **Unit:** schema/loader + round-trip (dokładanie `cachedAction` zachowuje
  komentarze/kolejność); ElementResolver z **zamockowanym agentem** (bez sieci);
  logika compile (idempotencja, drift/`compiledFrom`, walidacja unikalności).
- **Integracja:** mały **statyczny HTML** w repo + Playwright → `compile` buduje
  akcje, `render` produkuje `.mp4`; asercje: plik powstał, ma ścieżkę audio,
  sensowną długość.
- **CI:** LLM/agent **zawsze mockowany**; realny resolver tylko w opcjonalnym
  teście „na żądanie".

## 13. Stack

Python 3.12+, `uv`, Playwright (Python), `pydantic`, `ruamel.yaml`, `typer`,
`ffmpeg`. TTS i Reasoner za interfejsami (wymienne backendy).

## 14. Odłożone (YAGNI)

- Faza `record` (nagrywanie własnych akcji) — zaprojektowana, nieimplementowana w
  v1.
- Hybryda „Python wewnątrz YAML" (`run: |`).
- `--auto-heal` w praktyce (interfejs zostaje, domyślnie off).
- Scenariusze multi-tab / iframe.
- Montaż post-sync (rozciąganie pauz audio do znaczników).
- Dyktowanie narracji w trakcie `record`.

## 15. Kwestie do rozstrzygnięcia w implementacji

- Konkretny provider TTS na start (interfejs i tak wymienny).
- Dokładny format wywołania `codex exec` i parsowanie odpowiedzi (JSON w ostatniej
  wiadomości vs znacznik).
- Reprezentacja `wait` (czas vs warunek na elemencie).
- Parametry overlay (prędkość kursora, styl ripple/highlight) — wartości domyślne
  + możliwość nadpisania w scenariuszu.
