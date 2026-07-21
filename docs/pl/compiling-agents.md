# Agenci kompilujący

## Krótka odpowiedź

**Standardowe `guidebot compile` i `guidebot compile-set` używają tylko Codex CLI.**
Architektura Pythona udostępnia protokół `Reasoner`, lecz CLI nie ma opcji
`--reasoner`, `--model` ani gotowego adaptera innego modelu.

| Rola lub backend | Działa? | Zakres |
|---|---:|---|
| Człowiek | Tak | Projektuje trasę i przegląda wygenerowany sidecar. |
| Dowolny zewnętrzny agent | Tak, przy tworzeniu plików | Może przygotować scenariusze i manifest render-set poza Guidebotem. |
| Codex CLI | Tak | Jedyny reasoner podłączony do standardowych poleceń kompilacji. |
| Własny `Reasoner` | Programowo | Wymaga adaptera i własnego runnera Pythona. |
| Claude, Gemini, OpenCode, Ollama, bezpośrednie API | Brak gotowego adaptera | Nie można wybrać ich flagą CLI. |
| Playwright Chromium | Tak, ale to nie agent | Waliduje i wykonuje akcje na stronie. |
| `render` i `render-set` | Bez agenta | Odtwarzają sprawdzony sidecar i nie wywołują LLM. |

„Rozszerzalny” oznacza dziś rozszerzalny przez API Pythona, a nie przez ustawienie w
YAML czy zmienną środowiskową.

## Dwa znaczenia „generowania nawigacji”

1. **Utworzenie trasy** — wybór URL-i, kolejności operacji, konta testowego, narracji
   i oczekiwanego końca, a następnie zapis kroków w `*.scenario.yaml`.
2. **Kompilacja targetów** — na bieżącej stronie zamiana opisu „pole adresu e-mail”
   na bezpieczny target, na przykład rolę `textbox` o nazwie `E-mail`.

Guidebot używa AI wyłącznie do punktu 2. `navigate` jest bezpośrednim `page.goto`.
Nie istnieje polecenie `discover`, `record` ani „zbadaj serwis i wymyśl instrukcję”.

Zewnętrzny agent może utworzyć:

- pojedynczy `*.scenario.yaml`;
- scenariusz z `audioTracks` i kompletem `translations`;
- pełne scenariusze językowe i `*.render-set.yaml`.

Zawsze przejrzyj kolejność i skutki uboczne, zastąp sekrety zmiennymi środowiskowymi,
uruchom walidację i pozwól Guidebotowi wygenerować sidecary. Nie proś agenta o ręczne
pisanie `*.compiled.yaml`.

## Co widzi Codex

Dla kroku wymagającego targetu Guidebot przekazuje:

- zaufaną instrukcję autora;
- maksymalnie 200 bieżących, zwykle widocznych kandydatów;
- ich ID, rolę, nazwę dostępności, tag, prostokąt, widoczność, aktywność i krótkie
  pochodzenie DOM.

Wartości pól formularza nie są dołączane bezpośrednio. Tekst pokazany później przez
aplikację w DOM lub nazwie dostępności może jednak wejść do kolejnego snapshotu.

Codex zwraca dane: rodzaj akcji i strukturalny `Target` albo jawny błąd. Nie klika,
nie wpisuje, nie otwiera URL-i i nie steruje przeglądarką. Playwright buduje locator,
wymaga jednego zgodnego elementu, sprawdza jego właściwości, zamraża tożsamość i
wykonuje akcję.

## `teach`, wpisywanie i sekrety

`teach` może opisać jedną akcję na jednym elemencie. Oprócz kliknięcia i hover reasoner
może rozpoznać wpisanie jawnego, niewrażliwego literału:

```yaml
- teach: "Wpisz demo@example.com w pole E-mail"
```

Wtedy Codex musi zwrócić `type` oraz dokładny `inputText` będący fragmentem instrukcji.
Guidebot sprawdza wartość i target, zapisuje literal w sidecarze v2, a render odtwarza
go bez LLM.

Nie używaj tego mechanizmu dla haseł, tokenów, kodów, numerów kart ani innych sekretów.
Instrukcje i pola wyglądające na wrażliwe są odrzucane. Użyj:

```yaml
- enterText:
    into: "pole hasła"
    text: "${DEMO_PASSWORD}"
```

`enterText.text` nie trafia bezpośrednio do promptu ani sidecara.

## Popup nie jest osobną akcją agenta

Instrukcja powinna opisać kliknięcie otwierające popup:

```yaml
- teach: "Kliknij przycisk Otwórz logowanie"
```

Guidebot wykrywa nową stronę po kliknięciu, zapisuje `opens_popup: true`, przełącza
dalszą kompilację na popup i wraca do strony głównej po zamknięciu popupu. Nie dodawaj
kroku „przełącz okno”. Obsługiwany jest najwyżej jeden popup w całym scenariuszu;
nieoczekiwane okna powodują błąd.

## Ograniczone uruchomienie Codex CLI

Wbudowany reasoner uruchamia `codex exec`:

- efemerycznie, w katalogu tymczasowym;
- z sandboxem tylko do odczytu i bez zatwierdzania operacji;
- z pominięciem konfiguracji użytkownika i reguł repozytorium;
- bez web search, shella, browser/computer-use, aplikacji, pluginów, skills, MCP i
  subagentów;
- z limitem 60 sekund na próbę; ponowienia mogą wydłużyć obsługę jednego targetu.

Guidebot nie przekazuje `--model` i uruchamia Codexa z `--ignore-user-config`, więc
zmiana modelu w konfiguracji użytkownika nie wybiera go dla tego wywołania.

## Kiedy AI jest wywoływane

| Krok | AI podczas compile? | Uwagi |
|---|---:|---|
| `say` | Nie | Tylko narracja. |
| `navigate` | Nie | Bezpośrednie przejście pod URL. |
| `wait: 2` | Nie | Pauza czasowa. |
| `teach` | Przy braku aktualnego celu | Agent wybiera akcję i target; dla `type` zwraca niewrażliwy literal. |
| `click` / `hover` | Przy braku aktualnego celu | Rodzaj akcji jest stały, agent rozwiązuje target. |
| `enterText` | Przy braku aktualnego celu | Agent rozwiązuje tylko `into`. |
| `select` | Przy braku aktualnego celu | Agent rozwiązuje tylko `from`; `option` jest sprawdzane wobec listy rozwiązanego elementu, nie trafia do reasonera. |
| `highlight` | Przy braku aktualnego celu | Agent rozwiązuje `what`; krok nigdy nie dotyka strony. |
| warunkowy `wait` | Przy braku aktualnego celu | Agent rozwiązuje `until`. |
| aktualny target zweryfikowany na żywo | Nie | Compile może go ponownie użyć. |
| `render` / `render-set` | Nigdy | Stary sidecar powoduje błąd zamiast auto-heal. |

## Własny `Reasoner`

Adapter implementuje asynchroniczne `resolve(instruction, candidates)` i zwraca
`ReasonerResult` albo `ReasonerError`. Dla pojedynczego scenariusza użyj runnera, który
tworzy kontekst z właściwym `locale`:

```python
from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile_in_browser


async def compile_with_custom_reasoner(reasoner) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            await run_compile_in_browser(
                Path("scenarios/login.scenario.yaml"),
                browser,
                reasoner,
            )
        finally:
            await browser.close()
```

Dla zestawu językowego ten sam obiekt można przekazać do `run_compile_set`:

```python
from guidebot_recorder.recorder.render_set import run_compile_set
from guidebot_recorder.scenario.render_set import load_render_set

plan = load_render_set("scenarios/login.render-set.yaml")
await run_compile_set(plan, browser, reasoner)
```

Własny adapter nie pojawi się automatycznie w CLI. Warstwa wyboru backendu pozostaje
osobną funkcją do zaimplementowania.
