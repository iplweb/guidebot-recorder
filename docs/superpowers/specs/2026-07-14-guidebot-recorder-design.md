# Guidebot-recorder — design (spec v2)

Data: 2026-07-14
Status: zaakceptowany do napisania planu implementacji
Rewizja: v2 — po self-review dwóch niezależnych agentów (Fable + Codex);
wciągnięte poprawki krytyczne/istotne, patrz §17 (dziennik zmian).

## 1. Cel i zakres

Narzędzie, które z tekstowego opisu scenariusza (YAML) generuje **film
szkoleniowy**: bot wchodzi na stronę, przechodzi daną funkcję krok po kroku
(Playwright), pokazuje kursor i kliknięcia, a lektor (TTS) tłumaczy, co się
dzieje. Produktem wyjściowym jest **plik `.mp4` z narracją głosową**.

Rdzeń pomysłu to **kompilator**: scenariusz pisany intencjami po ludzku
(„kliknij Zaloguj") zostaje **skompilowany** przez AI do postaci z zamrożonymi,
konkretnymi namiarami na elementy. Dzięki temu właściwe renderowanie filmu jest
**deterministyczne w warstwie akcji** (patrz §2, granica gwarancji) i nie
wymaga LLM-a: przeglądarka przechodzi całą funkcję od początku do końca jednym
ciągiem, na świeżej sesji, bez doczepiania się w trakcie.

### Zakres v1
- Faza `compile` (intencje → wkompilowane akcje) z resolverem AI.
- Faza `render` (film `.mp4` z lektorem).

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

### Granica gwarancji determinizmu
**Deterministyczne są: akcje, ich typy i namiary na elementy** (zamrożone w
`cachedAction`) oraz **treść i długość narracji** (audio z cache, §8). **Nie są
gwarantowane co do klatki:** czasy ładowania stron, latencja sieci, animacje CSS
aplikacji docelowej. Powtarzalność renderu opiera się dodatkowo na przypiętym
**środowisku** (§16) i **`config`** scenariusza (viewport, język — §3.1).
„0×LLM" w renderze oznacza brak wywołań AI; nie oznacza braku I/O sieciowego do
aplikacji docelowej.

### Faza `compile` (`guidebot compile scenario.yaml`)
- Uruchamia scenariusz na **świeżej sesji** i wykonuje kroki **sekwencyjnie od
  początku** (patrz algorytm §5.6), bo resolver potrzebuje snapshotu strony *w
  stanie danego kroku*. Wszystkie akcje są realnie wykonywane (skutki uboczne —
  patrz wymagania wobec środowiska §16).
- Dla kroku wymagającego namiaru bez ważnego `cachedAction` woła
  **ElementResolver** (LLM/agent). Wynik — struktura namiaru + typ akcji —
  zostaje **wpisany w ten sam plik** pod danym krokiem jako `cachedAction`.
- To **jedyna** faza, w której działa AI. LLM **zwraca wyłącznie dane** (§5.5);
  wszystkie akcje w przeglądarce wykonuje Playwright.
- Edycja pliku jest **w miejscu** (round-trip, §4).

### Faza `render` (`guidebot render scenario.yaml`)
- **Faza 0 — przygotowanie audio (offline):** zanim otworzymy nagrywaną
  przeglądarkę, syntezujemy i **cache'ujemy całą narrację** (§8). Render nie woła
  TTS „na żywo".
- **0×LLM.** Czyta wkompilowane `cachedAction`, odtwarza kroki czystym
  Playwrightem. Przed każdą akcją **waliduje namiar na żywej stronie** (§5.4,
  render-time): jeśli locator nie trafia lub trafiony element nie zgadza się z
  zamrożoną tożsamością (`role`/`name`) → **twardy błąd** „re-compile" (render nie
  ma prawa wołać LLM-a).
- Świeża przeglądarka, całość jednym przejściem, brak doczepiania się.
- `--auto-heal`: **zarezerwowana nazwa, w v1 niezaimplementowana** (błąd „not
  implemented"). Docelowo osobna komenda naprawcza aktualizuje cache i restartuje
  render od kroku zero — nigdy LLM w trakcie nagrywania.

## 3. Format scenariusza (YAML deklaratywny)

Scenariusz to `config` (§3.1) + lista `steps`. YAML jest **formatem autorskim**;
pod spodem jest wspólne Python API (`Recorder`, §6), a runner YAML-a to jeden
frontend nad nim.

### 3.1 Nagłówek `config`
```yaml
config:
  title: "Logowanie do systemu"
  baseUrl: https://app.example.com     # opcjonalny prefiks dla navigate
  viewport: { width: 1280, height: 720 }  # = rozmiar wideo; wymagany dla powtarzalności
  tts: { provider: elevenlabs, voice: "pl-PL-Marek", lang: pl-PL }
```
`viewport` jest wymagany — determinuje zarówno powtarzalność namiarów, jak i
rozmiar `.mp4`.

### 3.2 Komendy

| Komenda | Znaczenie | Akcja | Namiar (cache) |
|---|---|---|---|
| `say` | Czysta narracja, nic nie robi | — | nie |
| `teach` | Lektor mówi całe zdanie-przewodnik; LLM wyłuskuje z niego akcję i ją wykonuje | tak (wnioskowana) | tak |
| `enterText` | Wpisanie tekstu w pole (jawna wartość) | type | tak (na `into`) |
| `navigate` | Przejście pod URL | goto | nie |
| `wait` | Pauza czasowa **lub** warunek na elemencie | — | tak, jeśli warunek elementowy |
| `click` / `hover` | Jawny escape-hatch (akcja bez narracji lub gdy narracja ≠ akcja) | click/hover | tak |

**Reguły struktury kroku** (walidowane przez pydantic, §12):
- **dokładnie jedna komenda na krok** (błąd, gdy np. `click` + `navigate` razem);
- opcjonalne pola towarzyszące: `say` (własna narracja przy `enterText`/`click`/
  `hover`), `cachedAction` (dokładany przez compile).

**Substytucja zmiennych:** wartości mogą zawierać `${ENV_VAR}` (np.
`text: "${DEMO_PASS}"`), rozwijane ze środowiska w compile/render — **sekrety nie
trafiają do repo**. Brak zmiennej → twardy błąd.

### 3.3 `teach` — workhorse
Wartość `teach` to **całe zdanie-przewodnik** („Aby się zalogować, należy kliknąć
przycisk Zaloguj w prawym górnym rogu"). Lektor wypowiada je w całości, a
kompilator:
1. **wyłuskuje część wykonawczą** ze zdania,
2. **wnioskuje typ akcji** (click / hover),
3. **rozwiązuje cel** do semantycznego namiaru (§5),
4. zapisuje wszystko w `cachedAction`.

Ograniczenia (kontrakt resolvera musi je sygnalizować):
- **0 akcji w zdaniu** (np. „Przyjrzyj się panelowi") → błąd compile „użyj `say`";
- **>1 akcja** (np. „kliknij A, potem B") → błąd compile „rozbij na kroki";
- **instrukcja czysto przestrzenna bez uchwytu semantycznego** → patrz §5.5.

`teach` obsługuje kliki/hovery. **Wpisywania** się przez `teach` nie robi (brak
jawnej wartości) — od tego jest `enterText`.

### 3.4 `wait`
Forma dyskryminowana:
```yaml
- wait: 2.0                              # sekundy (bez namiaru)
- wait: { until: "aż pojawi się tabela wyników", timeout: 10 }  # warunek → cachedAction jak teach
```

### Przykład — po `compile` (ten sam plik)
```yaml
config:
  title: "Logowanie"
  viewport: { width: 1280, height: 720 }
  tts: { provider: elevenlabs, voice: "pl-PL-Marek", lang: pl-PL }
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: https://app.example.com
  - teach: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu"
    cachedAction:
      action: click
      strategy: role
      role: button
      name: "Zaloguj"
      exact: true
      fingerprint: { commandKind: teach, compilerVersion: 1, compiledFrom: "Aby się zalogować, należy kliknąć przycisk Zaloguj w prawym górnym rogu" }
  - enterText: { into: "pole email", text: "${DEMO_EMAIL}" }
    say: "Teraz wpisuję swój adres e-mail."
    cachedAction:
      action: type
      strategy: role
      role: textbox
      name: "Email"
      exact: true
      fingerprint: { commandKind: enterText, compilerVersion: 1, compiledFrom: "pole email" }
```

## 4. Kompilacja in-place (jeden plik)

- **Jeden plik** — brak osobnego artefaktu „compiled".
- **Round-trip** przez `ruamel.yaml`: kompilator **mutuje bezpośrednio
  `CommentedMap`** (nie przepuszcza całości przez model pydantic przy zapisie),
  dokładając wyłącznie klucz `cachedAction`. Zachowanie formatowania, kolejności i
  komentarzy.
- **Wspierany podzbiór YAML** jest zdefiniowany (block/flow, cudzysłowy) i pokryty
  **golden-diff testami**; kotwice/aliasy poza zakresem.
- **Zapis atomowy:** plik tymczasowy w tym samym katalogu → walidacja → `rename`.
- **Idempotencja:** `compile` woła LLM tylko dla kroków bez ważnego
  `cachedAction`. `--force` przelicza wszystko.
- **Wykrywanie nieaktualności (drift), §4.1.**

### 4.1 Fingerprint i dryf
`cachedAction.fingerprint` zawiera: `commandKind` (rodzaj komendy), pola celu
(`compiledFrom`), `compilerVersion`. Krok jest re-resolvowany, gdy:
- zmienił się tekst instrukcji (`compiledFrom` ≠ aktualny),
- zmienił się **rodzaj komendy** (`click`→`hover` nie zachowa starego cache),
- wzrosła `compilerVersion` (zmiana schematu namiaru).

**Uwaga:** fingerprint wykrywa zmiany *w scenariuszu*, nie *dryf strony*. Przed
dryfem strony chroni **walidacja render-time** (§5.4): render porównuje tożsamość
trafionego elementu z zamrożoną i przy niezgodności twardo pada „re-compile".

### 4.2 Schemat `cachedAction` (strukturalny, wersjonowany)
- `action`: `click | hover | type` — zamrożony typ akcji.
- `strategy`: `role | text | label | testid` (allowlista).
- `role`, `name`, `exact` (domyślnie `true`), opcjonalnie `nth`, `scope`
  (zawężenie do przodka).
- `fingerprint` (§4.1).

**Brak `locator` jako stringu-wyrażenia.** Locator Playwrighta jest budowany
**wyłącznie w zaufanym kodzie** z pól strukturalnych — zero eval/parsowania.

## 5. Resolver (tylko w `compile`, wołany rzadko)

### 5.1 PageContext
Playwright wyciąga **accessibility-snapshot** aktualnej strony i buduje
**ograniczoną listę kandydatów** (elementy interaktywne + nagłówki), każdy z:
stabilnym ID, `role`, dostępną nazwą, **bounding-box**, ancestry (skrótowo),
widocznością/enabled. **Strategia przycinania** (viewport-only + interaktywne)
utrzymuje rozmiar wejścia w ryzach na dużych stronach.

### 5.2 Reasoner (wymienny backend)
Mapuje `(kandydaci, instrukcja) → {action, strategy, role, name, nth?, scope?}`
albo sygnał błędu (0/>1 akcji, brak uchwytu). Wybierany w configu.
- **Default: `codex exec`** — subskrypcja, zero kosztu API.
- Alternatywy (odłożone aż default działa): `claude -p`, `opencode`, Claude
  Messages API.

### 5.3 Kontrakt wywołania `codex exec` (§5.2 default)
- wywołanie **przypięte, read-only / bez narzędzi plikowych** (agent tylko
  rozumuje nad tekstem),
- wejście: **zredagowany** snapshot kandydatów (bez sekretów/wartości pól),
- wyjście: **ścisły, obramowany JSON** wg schematu (framed markers), parsowany
  rygorystycznie; osobne `stderr`,
- **timeout + anulowanie**, **ograniczona liczba prób**,
- odporność na prompt-injection: tekst ze strony jest *danymi*, nie instrukcją.

Mechanizm domyślny: **snapshot→agent (tekst)**. **CDP-attach** (interaktywne
badanie strony przez agenta) — odłożony, aż ścieżka domyślna działa.

### 5.4 Trust-but-verify (dwa poziomy)
**Compile-time** (przed zapisem do cache): trafiony locator musi:
- trafiać w **dokładnie 1** element (`exact: true` domyślnie — chroni przed
  substring-match `get_by_role(name=)`),
- być **widoczny** i **enabled/edytowalny** stosownie do akcji,
- mieć **typ zgodny z akcją** (np. `type` tylko na `textbox`).
Niepowodzenie → **re-prompt** (max 2 próby), potem **twardy błąd** z listą
kandydatów do doprecyzowania przez autora.

**Render-time** (§2): przed akcją locator musi trafiać w 1 element, a jego
`role`/`name` muszą zgadzać się z zamrożonymi. Niezgodność → twardy błąd
„re-compile".

### 5.5 Rola LLM — granica i wykonanie akcji
LLM/agent działa **wyłącznie w `compile`** i **zwraca tylko dane** (namiar + typ).
**Nigdy** nie steruje przeglądarką — walidację i wszystkie akcje (compile i
render) wykonuje Playwright. Instrukcje czysto przestrzenne bez uchwytu
semantycznego (np. sam „w prawym górnym rogu" bez nazwy) resolver rozwiązuje przez
geometrię kandydatów (§5.1) do namiaru z `nth`/`scope`; jeśli się nie da —
**jawny błąd** „instrukcja nieobsługiwana, doprecyzuj".

### 5.6 Algorytm `compile`
```
otwórz świeżą sesję; ustaw viewport z config
dla każdego kroku po kolei:
  jeśli navigate → wykonaj goto (Playwright)
  jeśli krok wymaga namiaru:
     jeśli ważny cachedAction (brak driftu) → użyj go
     w przeciwnym razie:
        zbierz kandydatów (PageContext)
        Reasoner → dane; waliduj compile-time (§5.4); re-prompt/błąd
        zapisz cachedAction do pliku (atomowo, §4)
  wykonaj akcję Playwrightem (by odsłonić stan dla kolejnych kroków)
  zastosuj regułę gotowości (§7.1) przed następnym krokiem
```

## 6. Silnik `Recorder` (Python API) i frontendy

- **`Recorder`** — jedyne miejsce, które „wie jak": `navigate / say / enter_text /
  click / hover / wait`. Sedno.
- **YAML runner** — iteruje kroki i woła `Recorder`; obsługuje `teach` i
  wkompilowane `cachedAction`.
- **Python API (v1):** przyjmuje **wyłącznie jawne, strukturalne namiary**
  (`click(role="button", name="Zaloguj")`). **Nie** ma `teach`/rozwiązywania LLM
  ani in-place cache — te są wyłącznie ścieżką YAML+compile. (Pełny frontend
  skryptowy z zamrażaniem namiarów — odłożony.)

## 7. Wizualizacja kursora i kliknięć — overlay w DOM

Playwright steruje programowo i **nie renderuje** kursora. Wstrzykujemy **sztuczny
kursor** (HTML/SVG) + animacje: płynny ruch do celu, „ripple" przy kliknięciu,
highlight elementu.

- **Overlay tylko w `render`** (w compile zanieczyszczałby accessibility-snapshot).
- **Re-inject przy każdej nawigacji:** `add_init_script`, bo pełne przejście
  niszczy DOM. Pozycja kursora utrzymywana **po stronie Pythona** i odtwarzana po
  załadowaniu nowego dokumentu.
- **`pointer-events: none`** na overlayu (inaczej przechwyci kliki bota); brak
  wpływu na layout.
- Element poza ekranem: **najpierw scroll do celu i oczekiwanie na stabilny
  bounding-box**, dopiero potem ruch kursora + ripple w **momencie realnej akcji**.

### 7.1 Reguła gotowości
Po `navigate` i po akcji wyzwalającej nawigację/aktualizację SPA: `wait_until`
(per-komenda, konfigurowalny) + krótkie ustabilizowanie (settle) przed kolejnym
krokiem. `navigate` i `click` z nawigacją mają jawny kontrakt zakończenia.

## 8. Narracja (TTS) i montaż audio

- **Pre-cache (Faza 0 renderu):** przed otwarciem nagrywanej przeglądarki
  syntezujemy i **walidujemy** każdy segment narracji, zapisując do cache (katalog
  build, np. `.guidebot/audio/<hash>.wav`; klucz = hash `tekst + provider + voice`).
  Render czyta z cache → brak wywołań sieciowych i „głuchych klatek" w trakcie
  nagrywania; awaria TTS ujawnia się **przed** renderem.
- **Model czasu — narracja steruje tempem:** długość każdego segmentu `T` jest
  **znana z cache** przed odtworzeniem. Krok z narracją: mów (start audio) →
  czekaj `T` → wykonaj akcję.
- **Montaż (K2 — wideo Playwrighta + audio bed):**
  - render nagrywa wideo wbudowane (`context.record_video`, WebM VFR),
  - offsety segmentów kotwiczymy do **jednego monotonicznego zegara** startu
    nagrywania,
  - po zamknięciu kontekstu **probujemy** finalne wideo (ffprobe: długość),
  - budujemy **audio bed** = cisze-wypełniacze + segmenty na wyliczonych offsetach,
  - **ffmpeg** miksuje bed z wideo z jawnie określonymi: sample rate, kodekami,
    `-shortest`/pad na końcu, trim/pad do długości wideo.
  - **Świadomy kompromis:** WebM VFR daje sync **przybliżony** (nie co do klatki);
    dopuszczalny, bo tempo narzuca narracja i pauzy `T`. Dokładny post-sync
    odłożony (§14).
- **`enterText`/akcja bez `say`:** krótka, **konfigurowalna** pauza (domyślnie np.
  0.5 s) zamiast pełnej ciszy sterowanej audio.

## 9. Przepływ pojedynczego kroku `teach` (render)
```
krok: teach: "Aby się zalogować, należy kliknąć Zaloguj w prawym górnym rogu"
       cachedAction: {action: click, strategy: role, role: button, name: "Zaloguj", exact: true}

RENDER (audio już w cache, długość T znana):
1. overlay: (opcjonalny napis/dymek), start audio segmentu
2. czekaj T                                   ← narracja steruje tempem
3. zbuduj locator z pól cachedAction (zaufany kod)
4. walidacja render-time: 1 trafienie + zgodność role/name (§5.4) — inaczej błąd
5. scroll do celu, czekaj na stabilny bbox
6. overlay: ruch kursora + ripple + highlight w momencie akcji
7. Playwright wykonuje cachedAction.action (click)
8. reguła gotowości (§7.1); offset segmentu zapisany do audio bed (§8)
```

## 10. Artefakty i układ projektu
```
moje-szkolenie/
  login.scenario.yaml      # source + wkompilowane akcje (jeden plik, w git)
  .guidebot/audio/         # cache TTS (build, poza git)
  out/login.mp4            # generowane przez `render`

guidebot_recorder/         # pakiet aplikacji (uv + pyproject)
  scenario/    # schema (pydantic) + loader + round-trip (ruamel) + ${ENV} + config
  recorder/    # Recorder (Python API) + YAML runner + reguła gotowości
  resolver/    # PageContext (kandydaci+geometria) + Reasoner (codex/...) + walidacja
  overlay/     # injektowany JS: sztuczny kursor + animacje (re-inject)
  tts/         # interfejs TTS + providerzy + cache
  video/       # nagrywanie + audio bed + mux (ffmpeg/ffprobe)
  cli.py       # compile / render / validate
```

## 11. Obsługa błędów (fail-loud, nigdy po cichu)
- **compile — 0/>1 kandydat, zła tożsamość, niezgodny typ:** re-prompt (max 2) →
  twardy błąd + lista kandydatów.
- **compile — `teach` 0/>1 akcji / instrukcja nieobsługiwana:** błąd z podpowiedzią.
- **render — brak/niezgodny `cachedAction`, locator nie trafia lub zła tożsamość:**
  twardy błąd „re-compile".
- **TTS padło:** błąd w Fazie 0 (przed nagrywaniem), nie cichy film bez głosu.
- **`${ENV_VAR}` brak:** twardy błąd.
- **Nawigacja/timeout Playwrighta:** propagujemy.
- **`--auto-heal` w v1:** błąd „not implemented".

## 12. Testy
- **Unit:** schema/loader + `${ENV}`; round-trip (golden-diff: dokładanie
  `cachedAction` zachowuje komentarze/kolejność/podzbiór YAML, zapis atomowy);
  Reasoner z **zamockowanym agentem**; walidacja compile-time (unikalność, exact,
  widoczność/enabled, zgodność typu); fingerprint/drift; walidator „jedna komenda
  na krok".
- **Integracja:** statyczny **HTML w repo** + Playwright → `compile` + `render`.
  Asercje **mocne, nie tylko „mp4 istnieje"**: ślad wykonanych akcji, tożsamość
  klikniętego elementu, obecność kursora w próbkowanych klatkach, offset i długość
  audio w granicach, powtórzony render pod przypiętym środowiskiem daje zgodny
  wynik.
- **CI:** LLM/agent **zawsze mockowany**; realny resolver tylko w teście „na
  żądanie".

## 13. Stack
Python 3.12+, `uv`, Playwright (Python), `pydantic`, `ruamel.yaml`, `typer`,
`ffmpeg`/`ffprobe`. TTS i Reasoner za interfejsami (wymienne backendy).

## 14. Odłożone (YAGNI)
- Faza `record` (nagrywanie własnych akcji) — zaprojektowana, nieimplementowana.
- Pełny Python-frontend z zamrażaniem namiarów (v1 = tylko jawne locatory, §6).
- Hybryda „Python wewnątrz YAML".
- `--auto-heal` (zarezerwowane, „not implemented").
- Dodatkowi providerzy Reasonera + CDP-attach (aż default `codex exec` działa).
- Scenariusze multi-tab / iframe.
- Dokładny post-sync audio (rozciąganie do znaczników, sync co do klatki).
- Maskowanie wartości wrażliwych na ekranie/napisach (v1 chroni tylko repo przez
  `${ENV_VAR}`).
- Dyktowanie narracji w trakcie `record`.

## 15. Środowisko docelowe — wymagania (dot. `compile` i powtarzalnego `render`)
`compile` i `render` **realnie wykonują akcje** na aplikacji docelowej (logowanie,
wpisy). Dlatego:
- wskazane **konto/środowisko testowe** o **resetowalnym stanie** (fixtures),
- dane wejściowe przez `${ENV_VAR}` (§3.2),
- **przypięty** viewport/język (`config`) dla powtarzalności namiarów i rozmiaru
  wideo.
Bez resetowalnego stanu re-compile środkowego kroku może zależeć od skutków
wcześniejszych — algorytm §5.6 zawsze odgrywa od kroku zero na świeżej sesji.

## 16. Kwestie do rozstrzygnięcia w implementacji
- Konkretny provider TTS na start (interfejs wymienny).
- Dokładny format framowania JSON w `codex exec` i redakcja snapshotu.
- Wartości domyślne overlay (prędkość kursora, styl ripple/highlight, pauza dla
  akcji bez `say`) — z możliwością nadpisania w `config`.
- Parametry ffmpeg (kodeki, sample rate) i próg akceptowalnego dryfu sync.

## 17. Dziennik zmian (v1 → v2, po self-review)
- **K1:** dodano pre-cache TTS (Faza 0), render bez wywołań TTS na żywo.
- **K2:** wybrano mechanizm montażu (wideo Playwrighta + audio bed, sync
  przybliżony); dodano kotwiczenie do monotonicznego zegara, probe, trim/pad.
- **K3:** `cachedAction` strukturalny/wersjonowany; usunięto `locator`-string.
- **I1/§15:** dodano wymagania wobec środowiska i skutki uboczne compile.
- **I2/§5.5:** doprecyzowano — LLM zwraca tylko dane, Playwright wykonuje.
- **I3/§4.1:** fingerprint (rodzaj komendy + wersja) + walidacja render-time dryfu.
- **I4/§5.4:** `exact: true` domyślnie + widoczność/enabled/zgodność typu + asercja
  tożsamości w renderze.
- **I5/§5.1,5.5:** kandydaci z geometrią/ancestry; obsługa/odrzucenie instrukcji
  przestrzennych.
- **I6/§5.3:** kontrakt `codex exec` (read-only, framed JSON, timeout, retry,
  redakcja).
- **I7/§3.4:** `wait` = czas + warunek elementowy (z cache).
- **I8/§7:** overlay re-inject, `pointer-events:none`, scroll+stabilny bbox.
- **I9/§3.1:** nagłówek `config` (viewport wymagany).
- **I10/§4:** mutacja `CommentedMap`, podzbiór YAML, zapis atomowy, golden-diff.
- **I11/§5.1:** przycinanie snapshotu.
- **I12/§6:** Python API v1 tylko z jawnymi locatorami.
- **Sekrety/§3.2:** substytucja `${ENV_VAR}`.
- **Drobne:** limit re-prompt; `teach` 0/>1 akcji; „jedna komenda na krok";
  `--auto-heal` „not implemented"; mocniejsze testy integracyjne.
