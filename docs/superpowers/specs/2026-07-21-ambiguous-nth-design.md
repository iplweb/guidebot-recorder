# Niejednoznaczny namiar — indeks liczy maszyna, nie model

Data: 2026-07-21
Zgłoszenie: [#51](https://github.com/iplweb/guidebot-recorder/issues/51)
Status: projekt po przeglądzie adwersaryjnym, gotowy do wdrożenia

## Problem

`compile` potrafi zamrozić namiar wskazujący **inny element niż opisany
w scenariuszu** i zakończyć się sukcesem. Nic w późniejszym przebiegu tego nie
wyłapuje: kontrola tożsamości przy renderze porównuje `tag` + `ancestry_digest`,
a te są identyczne dla rodzeństwa o tej samej strukturze przodków. Błąd wychodzi
dopiero, gdy człowiek obejrzy gotowy film.

Zgłoszenie dokumentuje pięć błędnych zamrożeń w jednym scenariuszu (formularz
kryteriów django-multiseek), wszystkie przy zielonej kompilacji. Dwa różne kroki
dostały **identyczny** target `role=textbox name='' nth=1`, więc obie wartości
trafiły do tego samego pola.

### Przyczyna źródłowa

Zgłoszenie opisuje objaw jako „wartość `nth` zgaduje LLM". Analiza promptu
pokazuje coś ostrzejszego: **model nie ma z czego jej wyliczyć**.

Snapshot przekazywany reasonerowi (`_build_prompt`, `resolver/reasoner.py`)
zawiera `id`, `role`, `name`, `tag`, `bbox`, `visible`, `enabled`, `ancestry`.
Nie zawiera żadnego indeksu. Model, proszony o `nth`, wykonuje arytmetykę na
tablicy JSON, którą widzi — a Playwright liczy trafienia **swojego** lokatora
`get_by_role(role, name=…)`. To dwa różne zbiory:

- snapshot jest przefiltrowany do viewportu, do ról z `CANDIDATE_ROLES`
  i obcięty limitem 200;
- lokator Playwrighta widzi wszystkie trafienia w dokumencie, także poza
  viewportem, i tylko te o pasującej roli i nazwie.

Model liczy w innej jednostce niż wykonawca. Przy pojedynczej kontrolce zgadza
się przypadkiem; przy formularzu z powtarzalnymi wierszami rozjeżdża się prawie
zawsze — co wyjaśnia, dlaczego wszystkie 14 wystąpień `nth` pochodziło z jednego,
najbardziej powtarzalnego scenariusza.

Schemat odpowiedzi wstrzykiwany do promptu (`_response_schema_json()`) zawiera
przy tym `nth` jako dozwolone pole `RoleTarget`, czyli **wprost zaprasza do
zgadywania**, oraz rekurencyjne `scope` — o którym prompt nie mówi ani jednego
zdania, więc model nie wie, kiedy i po co go użyć. Stąd statystyka ze
zgłoszenia: 14 × `nth`, 0 × `scope`.

Model ma natomiast w ręku `candidate.id` — skrót ścieżki DOM policzonej po
stronie zaufanego kodu. Schemat odpowiedzi go nie przewiduje.

## Rozwiązanie w skrócie

**Model mówi, *który* element ma na myśli. Maszyna liczy, *którym z kolei* on
jest.**

1. Odpowiedź reasonera zyskuje `candidateId`; schemat, który model widzi, traci
   `nth` — indeks przestaje być czymś, co da się wyrazić w odpowiedzi.
2. `compile` buduje lokator, liczy trafienia i wylicza indeks przez dopasowanie
   ścieżek DOM do wskazanego kandydata. Jedno trafienie → `nth` nie jest
   zamrażane wcale.
3. Zamrożona tożsamość zyskuje skrót ścieżki DOM. Kompilacja scenariusza
   z pozycyjnym namiarem **zawsze otwiera przeglądarkę** i sprawdza, czy
   zamrożony indeks wciąż trafia w ten sam element; rozjazd unieważnia wpis
   i wymusza ponowną rezolucję.
4. Prompt uczy reasonera `scope` — mechanizmu, który od początku działa
   w resolverze, ale o którym model nigdy się nie dowiedział.
5. Świeżo zamrożony namiar pozycyjny wypisuje ostrzeżenie z `plik:linia`.

### Świadomie przyjęta konsekwencja

Zmiana **zamienia część cichych pomyłek na twarde błędy kompilacji**. Dziś
niejednoznaczny namiar przechodzi zawsze (błędnie, ale przechodzi). Po zmianie
krok, dla którego model nie wskaże kandydata i nie zawęzi przez `scope`, po
wyczerpaniu prób kończy się `TargetResolutionError`. To jest cel, nie skutek
uboczny — ale trzeba go nazwać, bo scenariusze kompilujące się dziś mogą przestać.
Dlatego `MAX_REPROMPT` rośnie z 2 na 3 (patrz niżej).

## Decyzje projektowe

| Decyzja | Uzasadnienie |
|---|---|
| `nth` znika ze schematu **widzianego przez model**, ale zostaje w `Target` | `Target.nth` jest wyjściem compile'a zapisywanym przez zaufany kod, nie wejściem od modelu. |
| Indeks wylicza jedno wywołanie `evaluate_all`, nie N wywołań `evaluate` | Jedna wymiana z przeglądarką niezależnie od liczby trafień, ten sam zbiór i ta sama kolejność, którą indeksuje `.nth(i)`. |
| `dom_path_digest` jest **opcjonalne** i **nie bierze udziału** w `Identity.matches()` | Ścieżka zmienia się od dowolnego nowego elementu u przodków, więc jako kryterium tożsamości dawałaby **fałszywe alarmy**. Koszt fałszywego alarmu zależy od miejsca: w `compile` to jedna zbędna rezolucja (bezpieczna, kończy się poprawnym wynikiem), w `render`/`guide` to zatrzymany film. Dlatego sygnał żyje wyłącznie tam, gdzie jego pomyłka jest tania. Domyślne `None` zachowuje ważność wszystkich istniejących sidecarów: **żadnego recompile**. |
| Brak twardego błędu na `nth` bez `scope` | Po zmianie indeks jest zmierzony, nie zgadnięty, a jego kruchość pokrywa wykrywanie dryfu. Twardy błąd czyniłby nieskompilowalną stronę, której cel `compile` zna z całą pewnością. |
| Brak nowej składni w `*.scenario.yaml` | Zgodnie ze zgłoszeniem: scenariusz zostaje warstwą intencji. |
| `feedback` w protokole `Reasoner` ma wartość domyślną i jest przekazywany tylko gdy niepusty | W testach jest **40** atrap z sygnaturą `resolve(self, instruction, candidates)`, żadna nie przyjmuje `**kwargs`. Wymuszony parametr wysadziłby je wszystkie — ten rodzaj niezgodności zepsuł `main` przy PR #44. |
| Ścieżka DOM zyskuje indeks także pod shadow rootem | Bez tego nie jest unikalna (patrz niżej) — a po zmianie to ona przenosi intencję. |

### Odrzucone warianty

- **Samo ostrzeżenie przy niejednoznaczności** — zostawia człowiekowi wyłapanie
  problemu, czyli tryb, który zawiódł w BPP.
- **Twardy błąd na każdy pozycyjny namiar** — kasowałby własną naprawę.
- **Ścieżka DOM jako kryterium `Identity.matches()`** — fałszywe alarmy przy
  kosmetycznych zmianach strony psułyby działające rendery.
- **Wykrywanie dryfu w `guide`** — mimo że `guide/capture.py` woła
  `reuse_failure` przy otwartej przeglądarce i to tam błąd ze zgłoszenia został
  zauważony. Powód ten sam: fałszywy alarm zatrzymałby przewodnik, którego
  sidecar jest poprawny. `guide` nie ma jak naprawić dryfu — jedyne, co mógłby
  zrobić, to odmówić pracy.
- **Furtka `nth:` w kroku scenariusza albo flaga `--allow-ambiguous`.**

## Moduły

### `resolver/page_context.py` — jedna definicja ścieżki DOM

Dziś `domPath` i `nthOfType` są domknięciami wewnątrz
`_COLLECT_CANDIDATES_SCRIPT`, a skrót liczy się inline przy budowie
`Candidate`. Weryfikacja musi liczyć **dokładnie to samo**, więc obie definicje
wędrują do wspólnego miejsca.

#### Naprawa unikalności ścieżki (blokujące)

Obecny kod (`page_context.py`, wewnątrz `domPath`):

```js
const parent = current.parentElement;
let segment = tag;
if (parent) { segment += `:nth-of-type(${nthOfType(current)})`; }
```

Element leżący **bezpośrednio pod shadow rootem** nie ma `parentElement`
(rodzicem jest `ShadowRoot`), więc jego segment nie dostaje indeksu — i całe
takie rodzeństwo ma identyczną ścieżkę, a więc identyczne `Candidate.id`.
Zweryfikowane empirycznie: dwa różne `<button>` w tym samym shadow roocie
dostają `candidate-7a02c96572535b5f`.

Dziś to nieszkodliwe (id jest etykietą dla modelu). Po tej zmianie id **przenosi
intencję**, więc kolizja oznaczałaby ciche zamrożenie złego elementu — dokładnie
tę awarię, którą naprawiamy, tylko przeniesioną z LLM-a do zaufanego kodu.

Poprawka: indeks doklejamy **zawsze**. `nthOfType` chodzi po
`previousElementSibling`, które działa również dla elementów pod `ShadowRoot`,
więc wystarczy usunąć warunek:

```js
const segment = `${tag}:nth-of-type(${nthOfType(current)})`;
```

Skutek uboczny: ścieżki elementów pod shadow rootem oraz korzenia `html`
zyskują `:nth-of-type(1)`, więc **ich `Candidate.id` się zmienia**. Id nie jest
nigdzie utrwalane (żyje tylko w obrębie jednej rezolucji), więc nie ma migracji
— ale asercje na przybitych ścieżkach w `tests/unit/resolver/test_page_context.py`
trzeba zaktualizować.

#### Kształt wspólnego kodu

```python
#: Wspólne źródło ścieżki DOM: wyrażenie funkcyjne wstrzykiwane do trzech
#: skryptów. Rozjazd między zbieraniem kandydatów a weryfikacją oznaczałby, że
#: `compile` porównuje skróty policzone dwiema regułami — i nigdy nie trafia.
_DOM_PATH_JS = """..."""


def candidate_id_for_path(path: str) -> str:
    """Skrót ścieżki DOM — jedyna definicja identyfikatora kandydata."""
    return "candidate-" + sha256(path.encode("utf-8")).hexdigest()[:16]


async def candidate_ids_of(locator: Locator) -> list[str]:
    """Identyfikatory wszystkich trafień lokatora, w kolejności ``.nth(i)``."""
```

Wymagania implementacyjne:

1. **`nthOfTypeCache` (WeakMap) musi powstawać wewnątrz wstrzykiwanego
   wyrażenia, per wywołanie.** Wyniesiony na `window` przeżyje między
   `evaluate`, a jest liczony leniwie i nigdy nie unieważniany — więc po
   przesunięciu elementów w DOM zwracałby nieaktualne indeksy.
2. **Konsumentów jest trzy, nie dwa**: `collect_candidates` (tablica),
   `candidate_ids_of` (tablica) oraz `capture_identity` (**pojedynczy** element,
   `locator.evaluate`). Stała musi dać się użyć w obu kształtach.
3. `collect_candidates` przestaje liczyć skrót inline i woła
   `candidate_id_for_path`.

### `resolver/identity_capture.py`

`_CAPTURE_SCRIPT` dokłada `path` do zwracanego obiektu, a `capture_identity`
wypełnia `dom_path_digest` przez `candidate_id_for_path`. **Jedno `evaluate`, nie
dwa** — nie tylko dla wydajności (`reuse_failure` woła to na każdym kroku, także
w `render` i `guide`), ale dlatego, że dwa odczyty w dwóch momentach mogłyby
opisywać dwa różne stany strony.

`dom_path_digest` przechowuje **tę samą wartość** co `Candidate.id` (ten sam
prefiks i ten sam skrót), żeby porównania krzyżowe między jednym a drugim były
poprawne z definicji.

### `resolver/positional.py` (nowy) — arytmetyka indeksu

```python
@dataclass(frozen=True, slots=True)
class Pinned:
    """Namiar z indeksem zmierzonym, nie zgadniętym."""
    target: Target
    matches: int          # ile elementów trafia namiar bez ``nth``
    index: int | None     # ``None`` gdy trafienie było jedno


@dataclass(frozen=True, slots=True)
class PinFail:
    reason: Literal[
        "not_found", "not_pinnable", "no_candidate_id",
        "candidate_not_matched", "ambiguous_candidate_id",
    ]
    message: str          # szablon dla modelu — patrz „Kontrakt komunikatów"


async def pin_position(
    root: Page | Frame, target: Target, candidate_id: str | None
) -> Pinned | PinFail:
```

| warunek | wynik |
|---|---|
| target nie jest `RoleTarget` | `PinFail("not_pinnable")` — `nth` istnieje wyłącznie na `RoleTarget`, a `model_copy(update={"nth": …})` na pozostałych klasach **nie waliduje** i po cichu ustawiłby pole, które `_build_locator` zignoruje |
| 0 trafień bez `nth` | `PinFail("not_found")` |
| 1 trafienie | `Pinned(target bez nth, matches=1, index=None)` |
| ≥ 2, brak `candidate_id` | `PinFail("no_candidate_id")` |
| ≥ 2, id pasuje dokładnie raz | `Pinned(target z nth=i, matches=N, index=i)` |
| ≥ 2, id nie pasuje do żadnego | `PinFail("candidate_not_matched")` |
| ≥ 2, id pasuje więcej niż raz | `PinFail("ambiguous_candidate_id")` — fail-closed na wypadek, gdyby ścieżka mimo naprawy okazała się nieunikalna |

```python
async def pinned_drifted(root: Page | Frame, cached: CachedAction) -> bool:
    """Czy zamrożony indeks wskazuje dziś inny element niż przy kompilacji."""
```

Algorytm jest **jeden i jest przybity w specu**, bo dwa naturalne warianty dają
różne wyniki: budujemy lokator **bez** `nth`, pobieramy `candidate_ids_of` i
porównujemy element o indeksie `target.nth` z `cached.identity.dom_path_digest`.
Indeks poza zakresem (lista trafień się skurczyła) to **dryf**, nie wyjątek.

#### Ograniczenie: co ten sygnał łapie, a czego nie

Ustalone empirycznie przy wdrożeniu, wbrew pierwotnemu brzmieniu tego specu.
`dom_path_digest` jest ścieżką **pozycyjną i absolutną** (`nth-of-type` od
korzenia), więc:

| zmiana strony | wykryta? |
|---|---|
| element zniknął — lista trafień krótsza niż `nth` | **tak** |
| cel przeniesiony w inne miejsce struktury | **tak** |
| zmiana strukturalna gdziekolwiek nad gałęzią celu | **tak** (fałszywy alarm — kosztuje jedną zbędną rezolucję, patrz decyzje) |
| **jednorodne wstawienie rodzeństwa**, np. wiersz dołożony przed celem w tabeli identycznych wierszy | **nie** |

Ostatni wiersz jest istotny, bo to nagłówkowy scenariusz dryfu ze zgłoszenia
(„`nth: 3` przestaje działać przy pierwszym dodanym wierszu"). Element, który po
wstawieniu trafia w zamrożone `nth`, zajmuje **tę samą pozycję strukturalną** co
zamrożony, więc ma identyczny digest — mimo że logicznie jest to inny wiersz.
Ścieżka pozycyjna z definicji nie odróżnia „ten sam element" od „inny element na
tej samej pozycji".

Wniosek dla projektu, a nie dla implementacji: wykrywanie dryfu jest **częściowym
backstopem**, nie gwarancją. Trwałość w tym scenariuszu zapewnia `scope`
(reasoner jest go teraz uczony) — dokładnie tak, jak przewidywała Propozycja 2 ze
zgłoszenia — a ostrzeżenie o namiarze pozycyjnym kieruje autora w tę stronę.
Główna naprawa (poprawny `nth` **w chwili kompilacji**, mierzony z `candidateId`)
działa niezależnie od tego ograniczenia i to ona zamyka Propozycję 1.

Odrzucone wzmocnienie: unieważnianie reuse dla każdego gołego `nth` (bez
`scope`), czyli ponowna rezolucja przy każdej kompilacji. Zamknęłoby lukę, ale
kosztem wywołania LLM-a na każdy taki krok przy każdej kompilacji i utraty
determinizmu tam, gdzie cache istnieje właśnie po to, by go zapewnić.

`False` — czyli „nie ma czego sprawdzać" — dla: targetu bez `nth`, targetu
niebędącego `RoleTarget`, `cached.identity is None` (ukryte czekanie), oraz
sidecara sprzed tej zmiany (`dom_path_digest is None`). Cisza dla starych
artefaktów jest celowa: nie mają zamrożonej ścieżki, więc każdy werdykt poza
„nie wiem" byłby zmyślony.

### `resolver/reasoner.py` — model wskazuje kandydata

- `ReasonerResult` zyskuje `candidate_id: str | None = None`.
- **Rozluźnienie kontroli zbioru kluczy** (blokujące). Dziś
  `_result_from_payload` egzekwuje dokładne zbiory:
  `set(payload) not in ({"action","target"}, {"action","target","inputText"})`
  → `ValueError`. Odpowiedź z `candidateId` byłaby odrzucona, dwie wewnętrzne
  próby `CodexReasoner` by ją powtórzyły, a końcowy `ValueError` **nie jest**
  `TargetResolutionError`, więc `_compile_step` nie opakuje go w baner
  `plik:linia` i kompilacja padnie bez wskazania kroku. Nowe dopuszczalne zbiory:

  | gałąź | dopuszczalne klucze |
  |---|---|
  | `type` | `{action,target}`, `{action,target,inputText}`, `{action,target,candidateId}`, `{action,target,inputText,candidateId}` |
  | pozostałe akcje | `{action,target}`, `{action,target,candidateId}` |
  | błąd | `{error,message}` — bez zmian, `candidateId` w tej gałęzi to `ValueError` |

  `candidateId` musi być niepustym `str`; wartość spoza zbioru wysłanych
  kandydatów odrzuca dopiero `resolve_step_target` (ma listę, reasoner nie).
- `_response_schema_json()` usuwa `nth` z definicji `RoleTarget` w schemacie
  **przekazywanym modelowi** i dokłada `candidateId` do obu gałęzi sukcesu.
- `_result_from_payload` odrzuca `nth` na dowolnym poziomie `target`
  (rekurencyjnie przez `scope`) — `ValueError`, czyli wewnętrzna powtórka.
  Schemat jest tylko tekstem w promptcie; egzekucja jest tutaj.
- `Reasoner.resolve` zyskuje `feedback: str | None = None`. Zaktualizować także
  `RaisingReasoner.resolve` (`recorder/session.py`) — produkcyjną implementację
  protokołu. Runtime jest bezpieczny bez tego (rzuca `SetupNeedsCompile` przy
  pierwszym wywołaniu), ale sygnatury mają być spójne; CI nie ma type-checkera,
  więc rozjazd nie wysadziłby builda.
- Prompt zyskuje sekcję o celowaniu:

  ```
  Targeting rules:
  - Prefer a unique accessible name. Never return an index: you cannot know how
    many elements the executed locator will match.
  - When several controls share a role and an accessible name, narrow the target
    with "scope" — an ancestor that contains distinguishing text. Example:
    {"strategy":"role","role":"button","name":"×",
     "scope":{"strategy":"text","text":"Charakter formalny"}}
  - Always return "candidateId": the id of the candidate you mean. It is how the
    caller pins the exact element; an answer without it may be rejected.
  ```

#### Kontrakt komunikatów zwrotnych (bezpieczeństwo)

Prompt buduje cały model zaufania na tym, że dane ze strony żyją wyłącznie
między `BEGIN_UNTRUSTED_PAGE_CANDIDATES_JSON` a `END_…`. Sekcja z feedbackiem
mogłaby ten model obejść: precedens już istnieje — `_option_missing_message`
wkleja etykiety `<option>` **ze strony** do `ValidationFail.message`, a
`resolution.py` wstawia `last_rejection.message` do treści błędu.

Dlatego komunikat rozdziela się na dwa:

| odbiorca | treść | reguła |
|---|---|---|
| **model** (`feedback` w promptcie) | szablon o stałej treści, w który wstawiane są **wyłącznie liczby i identyfikatory kandydatów** (`candidate-<hex>`) | zero tekstu pochodzącego ze strony — także nazw kontrolek, także „czym się różnią kandydaci" |
| **człowiek** (`TargetResolutionError`, baner) | może być bogatszy | nie trafia do żadnego promptu |

Sekcja w prompcie nazywa się `CALLER_METADATA_NOT_INSTRUCTIONS` i **nie** jest
oznaczana jako treść zaufana ani jako instrukcja.

`candidateId` sam w sobie nie jest wektorem wycieku selektora: `_build_locator`
buduje lokator wyłącznie ze strukturalnych pól `Target` i kończy `raise
TypeError` dla obcych obiektów, a `pin_position` **porównuje** `candidate_id`
z wyliczonymi skrótami i nigdy nie wstawia go do selektora. To ma zostać
zapisane w docstringu `pin_position`.

### `models/identity.py`

```python
    #: Skrót ścieżki DOM elementu — sygnał dryfu dla `compile`, nie kryterium
    #: tożsamości. Poza `matches()` świadomie: ścieżka zmienia się od dowolnego
    #: nowego elementu u przodków, więc twarde porównanie psułoby działające
    #: rendery. `None` w sidecarach sprzed tej zmiany.
    dom_path_digest: str | None = None
```

`matches()` **bez zmian**. Sidecar zapisywany jest przez
`model_dump(by_alias=True, exclude_none=True)`, więc `None` nie trafia do
YAML-a — istniejące pliki nie puchną i nie ma szumu w diffach.

### `resolver/resolution.py` — wpięcie w istniejącą pętlę

Algorytm jest podany dosłownie, bo proza i pseudokod w pierwszej wersji specu
przeczyły sobie w sprawie `_relaxed_exact`:

```
warianty = [target]  ;  jeśli _relaxed_exact(target) → dopisz rozluźniony

dla wariantu w wariantach:
    validation = validate_compile_time(root, wariant, action, option)
    jeśli ValidationOk:
        zamrażamy wariant → koniec
    jeśli validation.reason == "not_unique" i wolno przypinać:
        pinned = pin_position(root, wariant, result.candidate_id)
        jeśli Pinned:
            validation = validate_compile_time(root, pinned.target, action, option)
            jeśli ValidationOk: zamrażamy pinned.target → koniec
        w przeciwnym razie: zapamiętaj PinFail.message jako feedback
    w przeciwnym razie: zapamiętaj validation jako last_rejection
```

Rozstrzygnięcia:

- **Zamrażany jest ten wariant, który przeszedł** — także wtedy, gdy jest to
  wariant z rozluźnionym `exact` (tak jak dziś: `render` musi zgadzać się
  z `compile`).
- Kombinacja „`exact=True` → `not_found`, `exact=False` → `not_unique`" **trafia
  do pinowania**. To nie jest przypadek egzotyczny: `_relaxed_exact` istnieje
  właśnie dlatego, że nazwa skopiowana z kandydata rozjeżdża się z matcherem
  Playwrighta.
- **„wolno przypinać" wyklucza `waitFor` ze stanem `hidden`.** Dla niego
  `identity` jest z założenia `None`, więc nie ma gdzie zapisać
  `dom_path_digest`; jednocześnie `reuse_failure` dla `hidden` wraca wcześnie
  (`count() <= 1 → None`), a locator z zamrożonym `.nth(n)` ma zawsze
  `count() ∈ {0,1}` — przypięty ukryty wait byłby więc **nieusuwalny**: ani
  `wait_ambiguous`, ani dryf by go nie unieważniły. Niejednoznaczne ukryte
  czekanie zostaje przy dotychczasowym zachowaniu (`not_unique` → re-prompt).
- `Pinned(index=None)` z pętli jest osiągalne tylko przez wyścig DOM
  (`validate_compile_time` widział ≥ 2, `pin_position` widzi 1). Traktujemy to
  jak sukces: rewalidujemy `pinned.target` (czyli wariant bez `nth`) i jeśli
  przechodzi — zamrażamy.
- `candidate_id` spoza zbioru wysłanych kandydatów jest odrzucane fail-closed
  i staje się feedbackiem.
- Feedback trafia do `reasoner.resolve(…, feedback=…)` przy kolejnym obrocie
  pętli, **tylko gdy jest niepusty** (zgodność ze starymi atrapami).
- **`MAX_REPROMPT` rośnie z 2 na 3.** Dziś `for _ in range(MAX_REPROMPT)` znaczy
  „dwie próby łącznie", czyli feedback dostawałby dokładnie jedną szansę
  naprawczą — za mało przy nowym, trudniejszym zadaniu, a każda porażka to
  twardy błąd kompilacji.

`ResolvedTarget` zyskuje **jedno** pole:

```python
    pinned: Pinned | None = None   # wartość domyślna jest wymagana
```

Wartość domyślna nie jest kosmetyką: `ResolvedTarget` to `frozen`/`slots`
dataclass z sześcioma polami bez domyślnych, konstruowana nazwanymi argumentami
w `tests/unit/recorder/test_render_highlight_dispatch.py`
i `tests/unit/recorder/test_selects_wiring.py` — bez `= None` obie się wywalą.
Jedno pole zamiast `positional: bool` dlatego, że baner ma brzmieć „2 z 11
pasujących", a samego `bool`-a ani `target.nth` nie da się w liczbę trafień
zamienić.

### `recorder/compile.py`

- **`compile_up_to_date` musi zwrócić `False`, gdy którykolwiek zamrożony target
  niesie `nth`** (rekurencyjnie przez `scope`). Bez tego CLI kończy pracę
  komunikatem „nic do skompilowania" **bez otwierania przeglądarki** — odcisk
  kroku nie zmienia się od przebudowy strony — i wykrywanie dryfu jest martwe
  na jedynej ścieżce, którą używa człowiek. Koszt: scenariusz z pozycyjnym
  namiarem otwiera przeglądarkę przy każdej kompilacji. To dokładnie ten
  scenariusz, który cicho gnije, więc koszt jest celowany.
- Przy reuse: `pinned_drifted(page, cached_in)` obok `reuse_is_valid`; dryf
  unieważnia wpis, a świeża rezolucja mierzy indeks od nowa.
- Po świeżej rezolucji z `resolved.pinned` niosącym indeks: baner ostrzegawczy
  przez `step_banner(..., warning=True)`, tą samą drogą co `_warn_absent`:
  `namiar pozycyjny (2 z 11 pasujących) — rozważ doprecyzowanie opisu, żeby
  wskazywał element jednoznacznie`.
- `_target_desc` pokazuje `scope` **i `nth`** — inaczej `--verbose` ukrywa
  dokładnie to, co dodajemy.

## Kompatybilność wsteczna

| Artefakt | Skutek |
|---|---|
| Sidecar bez `dom_path_digest` | Ważny. `pinned_drifted` zwraca `False`, reuse działa jak dotąd. |
| Sidecar z `nth` sprzed zmiany | Ważny. Pierwsza kompilacja z tą zmianą otworzy przeglądarkę (patrz `compile_up_to_date`), ale bez zamrożonej ścieżki dryfu nie wykryje. Ponowna rezolucja (`--force` albo zmiana kroku) zmierzy indeks i dopisze ścieżkę. |
| `Identity.matches()` | Bez zmian — `render` i `guide` zachowują się identycznie. |
| `COMPILER_VERSION` | **Bez podbicia.** Podbicie wymusiłoby recompile wszystkiego dla zmiany, która sama się goi. |
| 40 atrap `Reasoner` w testach | Bez zmian — `feedback` ma wartość domyślną i jest przekazywany tylko gdy niepusty. |
| `Candidate.id` dla shadow DOM | Zmienia się (naprawa unikalności). Id nie jest utrwalane; do poprawienia asercje w `test_page_context.py`. |

## Testy

### Jednostkowe

`tests/unit/resolver/test_positional.py` (nowy, na prawdziwym Chromium jak
`test_validate.py`). Fixture odtwarza dowód ze zgłoszenia: trzy wiersze
z przyciskiem `×` **bez nazwy dostępnej** (ikona `aria-hidden`, bo
`<button>×</button>` **ma** nazwę `"×"` — `accessibleName` bierze
`textAlternative` dla roli `button`) oraz wiersz „Zakres lat" z dwoma
nienazwanymi polami tekstowymi.

- jedno trafienie → `Pinned` bez `nth`;
- trzy trafienia + id trzeciego → `nth=2`;
- brak `candidate_id` → `no_candidate_id`; id spoza trafień →
  `candidate_not_matched`; zero trafień → `not_found`; `TextTarget` →
  `not_pinnable`;
- **dwa różne kroki roku dostają różne `nth`** — regresja na najbardziej
  wymowny przypadek ze zgłoszenia;
- `pinned_drifted`: `False` dla świeżo zamrożonego, `True` gdy cel zmienia
  pozycję strukturalną, `True` gdy lista trafień skurczyła się poniżej `nth`,
  `False` dla sidecara bez `dom_path_digest` i dla `identity is None`.
  **Uwaga**: wstawienie strukturalnie identycznego wiersza przed celem `True`
  **nie** da — patrz „Ograniczenie: co ten sygnał łapie, a czego nie". Test ma
  wymuszać dryf zmianą, która realnie przesuwa ścieżkę pozycyjną celu,
  i komentarzem tłumaczyć, dlaczego wariant jednorodny by nie zadziałał.

`tests/unit/resolver/test_page_context.py` — **unikalność `Candidate.id`
w obecności shadow DOM** (test, którego dziś brakuje; bez naprawy `domPath`
czerwony). Istniejący pomocnik liczący id i asercje na przybitych ścieżkach
zostają zaktualizowane, nie dublowane. `candidate_ids_of` zgadza się co do
kolejności z `locator.nth(i)`.

`tests/unit/resolver/test_reasoner.py` — schemat dla modelu nie zawiera `nth`
i zawiera `candidateId`; payload z `candidateId` **przechodzi** (regresja na
blokera); payload z `nth` (także w `scope`) odrzucony; `feedback` trafia do
promptu i nie jest oznaczony jako zaufany; atrapa bez parametru `feedback` nadal
działa. Parametryzacja `("nth", "1")` w
`test_resolve_rejects_coercible_but_schema_invalid_target_fields_twice` traci
sens (zacznie przechodzić z innego powodu) — zamienić na `exact`.

`tests/unit/resolver/test_resolution.py` — `not_unique` + poprawne `candidateId`
→ `ResolvedTarget` z `pinned`; `PinFail` → re-prompt z feedbackiem, a po
wyczerpaniu prób `TargetResolutionError` cytujący powód; `candidate_id` spoza
zbioru odrzucone; `waitFor`/`hidden` **nie** jest przypinany.

`tests/unit/recorder/test_compile.py` — `compile_up_to_date` zwraca `False` dla
sidecara z `nth`; dryf unieważnia reuse; baner ostrzegawczy dla namiaru
pozycyjnego zawiera liczbę trafień.

### Integracyjny

`tests/integration/test_ambiguous_targets.py` (nowy) — formularz o powtarzalnych
wierszach, pełny cykl `compile` → `render`. Dwa wymagania, bez których test
świeciłby na zielono nic nie dowodząc:

1. **Dowód negatywny.** Sam test z atrapą zwracającą poprawne `candidateId`
   mierzy tylko arytmetykę `pin_position`. Potrzebna jest para: atrapa „w starym
   stylu" (zgadnięty `nth`, jak robił to model) trafia w zły element, a nowa —
   w element opisany scenariuszem.
2. **Ścieżka produkcyjna.** Test dryfu musi przejść przez `run_compile`
   (tę samą bramkę `compile_up_to_date`, co CLI), a nie przez
   `run_compile_in_browser` — inaczej ominie dokładnie ten bloker, który
   naprawiamy.
3. **Dryf tylko w wariancie faktycznie wykrywanym.** Zmiana strony między
   kompilacjami ma przesuwać cel **strukturalnie** (albo go usuwać), a nie
   dokładać jednorodny wiersz — ten drugi wariant z założenia nie daje sygnału
   i test asertujący go byłby fałszywy. Uzasadnienie w „Ograniczenie: co ten
   sygnał łapie, a czego nie".

## Dokumentacja

Cztery pliki opisują dziś `nth` jako kruchy indeks pozycyjny bez gwarancji
i stają się nieaktualne: `docs/{pl,en}/troubleshooting.md`,
`docs/{pl,en}/scenario-reference.md`. Do tego `docs/{pl,en}/how-it-works.md`
(kto ustala indeks i po co istnieje `scope`) oraz `docs/{pl,en}/scenario-files.md`
(linia „look for unexpectedly broad targets, `nth` use" zyskuje kontekst).
CI buduje dokumentację z `--strict`.

## Poza zakresem

- XPath/CSS w `*.scenario.yaml` — wprost odrzucone w zgłoszeniu.
- Ścieżka DOM jako kryterium `Identity.matches()`.
- Wykrywanie dryfu w `render` i `guide` — uzasadnienie w „Odrzucone warianty".
