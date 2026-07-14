# Guidebot-recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zbudować narzędzie, które kompiluje scenariusz YAML (intencje po ludzku) do zamrożonych akcji, a następnie deterministycznie renderuje film szkoleniowy `.mp4` z kursorem i lektorem TTS.

**Architecture:** Kompilator dwufazowy. `compile` uruchamia Playwright, woła Reasoner (LLM/Codex) tylko dla kroków bez ważnego namiaru, wpisuje `cachedAction` w miejscu do tego samego YAML. `render` czyta zamrożone akcje, odtwarza je bez LLM, nagrywa wideo Playwrighta + wstrzyknięty overlay kursora, a narrację TTS (pre-cache) miksuje ffmpegiem. Wszystkie moduły odwołują się do jednego normatywnego modelu danych (`models/`, §4.3 specu).

**Tech Stack:** Python 3.12+, uv, Playwright (Python), pydantic v2, ruamel.yaml, typer, edge-tts (domyślny provider TTS, bez klucza), ffmpeg/ffprobe, pytest + pytest-asyncio.

## Global Constraints

- Python **3.12+**; zarządzanie zależnościami przez **uv** (`pyproject.toml`).
- Wszystkie typy danych to **jeden model pydantic v2** w `guidebot_recorder/models/` — inne moduły importują stąd, nie redefiniują.
- **`render` nie wywołuje LLM/AI** (0×LLM). LLM tylko w `compile`.
- **Determinizm:** locator budowany wyłącznie z pól strukturalnych (zero `eval`/parsowania stringów-wyrażeń).
- **Fail-loud:** żadnych cichych fallbacków; brakująca zmienna `${ENV_VAR}`, brak `cachedAction`, niezgodna tożsamość, awaria TTS → twardy błąd.
- **`${ENV_VAR}`** rozwijana tylko w `enterText.text` i `navigate`; zakaz w `say`/`teach`/`enterText.into`/`wait.until`; escape `$${`.
- **Schemat zamknięty:** nieznane klucze w `config`/kroku → błąd walidacji (`model_config = ConfigDict(extra="forbid")`).
- **TDD:** każdy task = test-first, częste commity. Testy LLM/sieci **zawsze mockowane** w CI.
- Styl commitów: `feat:`/`test:`/`chore:` + zakres modułu.

---

## File Structure

```
guidebot_recorder/
  __init__.py
  models/            # §4.3 normatywny model danych — FUNDAMENT (Faza 1)
    __init__.py
    target.py        # Target (unia dyskryminowana po strategy), Scope
    identity.py      # Identity + equality
    action.py        # CachedAction, Fingerprint, Expect, WaitState, ActionKind
    config.py        # Config, Viewport, TtsConfig, config_hash()
    scenario.py      # Scenario, Step (walidator "jedna komenda"), komendy
  scenario/          # I/O YAML (Faza 2A)
    __init__.py
    env.py           # substitute_env() — pola wartości + escape
    loader.py        # load_scenario() → (Scenario, CommentedMap)
    roundtrip.py     # inject_cached_action(), atomic_write()
  resolver/          # warstwa LLM (Faza 2B — może Codex)
    __init__.py
    page_context.py  # Candidate, collect_candidates()
    identity_capture.py  # capture_identity() z ElementHandle
    reasoner.py      # Reasoner (Protocol), CodexReasoner, ReasonerResult
    validate.py      # validate_compile_time()
  overlay/           # kursor (Faza 2C — może Codex)
    __init__.py
    cursor.js        # wstrzykiwany JS
    overlay.py       # Overlay: install(), ensure(), move_to(), ripple()
  tts/               # narracja (Faza 2D)
    __init__.py
    base.py          # TtsProvider (Protocol), Segment, TtsCache, cache_key()
    edge.py          # EdgeTtsProvider (domyślny)
  video/             # nagrywanie + mux (Faza 2E)
    __init__.py
    audiobed.py      # build_audio_bed()
    mux.py           # probe_duration(), mux()
  recorder/          # integracja (Faza 3)
    __init__.py
    recorder.py      # Recorder (Python API) + readiness
    compile.py       # run_compile()
    render.py        # run_render()
  cli.py             # typer: compile / render / validate  (Faza 4)
tests/
  unit/...           # per moduł
  integration/
    fixtures/app.html
    test_compile_render.py
pyproject.toml
```

**Dependency graph (co blokuje co):**
```
Faza0 scaffold → Faza1 models → ┬─ 2A scenario ─┐
                                 ├─ 2B resolver ─┤
                                 ├─ 2C overlay ──┼→ Faza3 recorder/compile/render → Faza4 cli + e2e
                                 ├─ 2D tts ──────┤
                                 └─ 2E video ────┘
```
Fazy **2A–2E są wzajemnie niezależne** (rozłączne katalogi) → równoległe. 2B i 2C to dobrzy kandydaci na Codex (samodzielne, dobrze zdefiniowane wejście/wyjście).

---

## Faza 0 — Scaffolding (sekwencyjnie, fundament)

### Task 0: Projekt uv + szkielet pakietu + pytest

**Files:**
- Create: `pyproject.toml`, `guidebot_recorder/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`
- Create: `README.md`

**Interfaces:**
- Produces: instalowalny pakiet `guidebot_recorder`, działające `pytest`.

- [ ] **Step 1: pyproject.toml**

```toml
[project]
name = "guidebot-recorder"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "playwright>=1.47",
  "pydantic>=2.7",
  "ruamel.yaml>=0.18",
  "typer>=0.12",
  "edge-tts>=6.1",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: pusty pakiet + smoke test**

`guidebot_recorder/__init__.py`:
```python
__version__ = "0.1.0"
```
`tests/unit/test_smoke.py`:
```python
import guidebot_recorder

def test_import():
    assert guidebot_recorder.__version__ == "0.1.0"
```

- [ ] **Step 3: instalacja + uruchomienie**

Run: `uv sync && uv run playwright install chromium && uv run pytest -q`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore: scaffold uv project + pytest"
```

---

## Faza 1 — Model danych (sekwencyjnie, FUNDAMENT §4.3)

> To jest kontrakt konsumowany przez wszystkie moduły. Musi być gotowy i stabilny przed fazą 2.

### Task 1: `models/target.py` — unia `Target`

**Files:**
- Create: `guidebot_recorder/models/__init__.py`, `guidebot_recorder/models/target.py`
- Test: `tests/unit/models/test_target.py`

**Interfaces:**
- Produces:
  - `RoleTarget(strategy: Literal["role"], role: str, name: str, exact: bool = True, nth: int | None = None, scope: "Target" | None = None)`
  - `TextTarget(strategy="text", text: str, exact: bool = True, scope=None)`
  - `LabelTarget(strategy="label", label: str, exact: bool = True, scope=None)`
  - `TestidTarget(strategy="testid", testid: str, scope=None)`
  - `Target = Annotated[Union[RoleTarget, TextTarget, LabelTarget, TestidTarget], Field(discriminator="strategy")]`

- [ ] **Step 1: Test dyskryminacji i rekurencyjnego `scope`**

```python
from pydantic import TypeAdapter
from guidebot_recorder.models.target import Target, RoleTarget

TA = TypeAdapter(Target)

def test_role_target_defaults():
    t = TA.validate_python({"strategy": "role", "role": "button", "name": "Zaloguj"})
    assert isinstance(t, RoleTarget) and t.exact is True and t.nth is None

def test_scope_is_recursive():
    t = TA.validate_python({
        "strategy": "role", "role": "button", "name": "OK",
        "scope": {"strategy": "testid", "testid": "dialog"},
    })
    assert t.scope.strategy == "testid"

def test_unknown_key_forbidden():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TA.validate_python({"strategy": "role", "role": "b", "name": "x", "bogus": 1})
```

- [ ] **Step 2: Run — FAIL** (`uv run pytest tests/unit/models/test_target.py -q`) — moduł nie istnieje.

- [ ] **Step 3: Implementacja**

```python
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: "Target | None" = None

class RoleTarget(_Base):
    strategy: Literal["role"] = "role"
    role: str
    name: str
    exact: bool = True
    nth: int | None = None

class TextTarget(_Base):
    strategy: Literal["text"] = "text"
    text: str
    exact: bool = True

class LabelTarget(_Base):
    strategy: Literal["label"] = "label"
    label: str
    exact: bool = True

class TestidTarget(_Base):
    strategy: Literal["testid"] = "testid"
    testid: str

Target = Annotated[
    Union[RoleTarget, TextTarget, LabelTarget, TestidTarget],
    Field(discriminator="strategy"),
]
for _m in (RoleTarget, TextTarget, LabelTarget, TestidTarget):
    _m.model_rebuild()
```

- [ ] **Step 4: Run — PASS. Step 5: Commit** `feat(models): Target discriminated union`

### Task 2: `models/identity.py` — `Identity` + równość

**Files:** Create `guidebot_recorder/models/identity.py`; Test `tests/unit/models/test_identity.py`

**Interfaces:**
- Produces: `Identity(tag: str, testid: str | None = None, href: str | None = None, ancestry_digest: str, identity_version: int = 1)`; metoda `matches(other: Identity) -> bool` (wszystkie obecne pola równe **i** `identity_version` równa).

- [ ] **Step 1: Test**

```python
from guidebot_recorder.models.identity import Identity

def test_matches_equal():
    a = Identity(tag="button", ancestry_digest="d1")
    assert a.matches(Identity(tag="button", ancestry_digest="d1"))

def test_mismatch_on_field():
    a = Identity(tag="button", testid="x", ancestry_digest="d1")
    assert not a.matches(Identity(tag="button", testid="y", ancestry_digest="d1"))

def test_version_mismatch():
    a = Identity(tag="a", ancestry_digest="d", identity_version=1)
    assert not a.matches(Identity(tag="a", ancestry_digest="d", identity_version=2))
```

- [ ] **Step 2: FAIL. Step 3: Implementacja**

```python
from pydantic import BaseModel, ConfigDict

class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str
    testid: str | None = None
    href: str | None = None
    ancestry_digest: str
    identity_version: int = 1

    def matches(self, other: "Identity") -> bool:
        return (
            self.identity_version == other.identity_version
            and self.tag == other.tag
            and self.testid == other.testid
            and self.href == other.href
            and self.ancestry_digest == other.ancestry_digest
        )
```

- [ ] **Step 4: PASS. Step 5: Commit** `feat(models): Identity + equality`

### Task 3: `models/action.py` — `CachedAction`, `Fingerprint`, enumy

**Files:** Create `guidebot_recorder/models/action.py`; Test `tests/unit/models/test_action.py`

**Interfaces:**
- Produces:
  - `ActionKind = Literal["click","hover","type","waitFor"]`
  - `Expect = Literal["navigation","idle","none"]`
  - `WaitState = Literal["visible","hidden","enabled"]`
  - `Fingerprint(command_kind: str, compiled_from: str, expect: Expect, compiler_version: int, config_hash: str, state: WaitState | None = None)`
  - `CachedAction(action: ActionKind, target: Target, identity: Identity | None, expect: Expect, fingerprint: Fingerprint, state: WaitState | None = None)` (identity opcjonalna — `waitFor:hidden` jej nie ma)

- [ ] **Step 1: Test** — konstrukcja click z Target+Identity; waitFor ze `state` bez identity.

```python
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.models.identity import Identity

def test_click_action():
    ca = CachedAction(
        action="click",
        target=RoleTarget(role="button", name="Zaloguj"),
        identity=Identity(tag="button", ancestry_digest="d"),
        expect="navigation",
        fingerprint=Fingerprint(command_kind="teach", compiled_from="...",
                                expect="navigation", compiler_version=1, config_hash="c"),
    )
    assert ca.action == "click" and ca.identity.tag == "button"

def test_waitfor_hidden_without_identity():
    ca = CachedAction(
        action="waitFor", state="hidden",
        target=RoleTarget(role="dialog", name="X"), identity=None, expect="none",
        fingerprint=Fingerprint(command_kind="wait", compiled_from="...",
                                expect="none", compiler_version=1, config_hash="c", state="hidden"),
    )
    assert ca.state == "hidden" and ca.identity is None
```

- [ ] **Step 2: FAIL. Step 3: Implementacja** (BaseModel `extra="forbid"`, `COMPILER_VERSION = 1` stała modułowa). **Step 4: PASS. Step 5: Commit** `feat(models): CachedAction + Fingerprint`

### Task 4: `models/config.py` — `Config` + `config_hash()`

**Files:** Create `guidebot_recorder/models/config.py`; Test `tests/unit/models/test_config.py`

**Interfaces:**
- Produces: `Viewport(width:int,height:int)`, `TtsConfig(provider:str, voice:str, lang:str, model:str|None=None, speed:float|None=None)`, `Config(title:str, viewport:Viewport, tts:TtsConfig, base_url:str|None=None, locale:str|None=None)`, funkcja `config_hash(cfg: Config) -> str` (SHA-256 z kanonicznej projekcji: `viewport.width/height`, `locale`, `tts.lang`; `CONFIG_HASH_VERSION=1` w salt).

- [ ] **Step 1: Test** — stabilność i wrażliwość hasha.

```python
from guidebot_recorder.models.config import Config, Viewport, TtsConfig, config_hash

def _cfg(w=1280, locale="pl-PL"):
    return Config(title="t", viewport=Viewport(width=w, height=720),
                  locale=locale, tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"))

def test_hash_stable():
    assert config_hash(_cfg()) == config_hash(_cfg())

def test_hash_changes_on_viewport():
    assert config_hash(_cfg(w=1280)) != config_hash(_cfg(w=768))
```

- [ ] **Step 2: FAIL. Step 3: Implementacja** — projekcja do dict z posortowanymi kluczami, `json.dumps(sort_keys=True)`, `hashlib.sha256`. **Step 4: PASS. Step 5: Commit** `feat(models): Config + config_hash`

### Task 5: `models/scenario.py` — `Scenario`, `Step`, walidator „jedna komenda"

**Files:** Create `guidebot_recorder/models/scenario.py`; Test `tests/unit/models/test_scenario.py`

**Interfaces:**
- Produces:
  - `EnterText(into: str, text: str)`
  - `WaitUntil(until: str, state: WaitState = "visible", timeout: float = 10.0)`
  - `Step(say: str|None=None, teach: str|None=None, enter_text: EnterText|None=None (alias "enterText"), navigate: str|None=None, click: str|None=None, hover: str|None=None, wait: float|WaitUntil|None=None, expect: Expect|None=None, cached_action: CachedAction|None=None (alias "cachedAction"))` — walidator modelowy: **dokładnie jedna** z komend {say, teach, enterText, navigate, click, hover, wait}; `say` może współtowarzyszyć akcji (`enterText/click/hover`).
  - `Scenario(config: Config, steps: list[Step])`
  - `Step.command_kind() -> str` i `Step.requires_target() -> bool`.

- [ ] **Step 1: Test** — poprawny scenariusz; błąd przy dwóch komendach; `say` obok `enterText` OK.

```python
import pytest
from pydantic import ValidationError
from guidebot_recorder.models.scenario import Scenario, Step

def test_single_command_ok():
    s = Step.model_validate({"teach": "kliknij X"})
    assert s.command_kind() == "teach" and s.requires_target()

def test_two_commands_forbidden():
    with pytest.raises(ValidationError):
        Step.model_validate({"click": "X", "navigate": "http://x"})

def test_say_with_action_ok():
    s = Step.model_validate({"enterText": {"into": "email", "text": "a@b"}, "say": "wpisuję"})
    assert s.command_kind() == "enterText" and s.say

def test_pure_say_needs_no_target():
    assert not Step.model_validate({"say": "witaj"}).requires_target()
```

- [ ] **Step 2: FAIL. Step 3: Implementacja** — `@model_validator(mode="after")` liczy nie-None komendy (`say` liczone tylko gdy jest jedyną). `requires_target()` = command_kind in {teach, enterText, click, hover} lub (wait i wait to WaitUntil). **Step 4: PASS. Step 5: Commit** `feat(models): Scenario + Step validation`

---

## Faza 2A — `scenario/` I/O YAML (równolegle)

### Task 6: `scenario/env.py` — substytucja `${ENV_VAR}`

**Files:** Create `guidebot_recorder/scenario/__init__.py`, `guidebot_recorder/scenario/env.py`; Test `tests/unit/scenario/test_env.py`

**Interfaces:**
- Consumes: —
- Produces: `substitute_env(value: str, env: Mapping[str,str]) -> str` (zamienia `${VAR}`, escape `$${` → literal `${`, brak zmiennej → `KeyError`); `substitute_scenario_values(raw: dict, env) -> dict` — stosuje tylko do `enterText.text` i `navigate`.

- [ ] **Step 1: Test**

```python
import pytest
from guidebot_recorder.scenario.env import substitute_env

def test_basic():
    assert substitute_env("${A}/x", {"A": "1"}) == "1/x"

def test_escape():
    assert substitute_env("$${A}", {}) == "${A}"

def test_missing_raises():
    with pytest.raises(KeyError):
        substitute_env("${NOPE}", {})
```

- [ ] **Step 2: FAIL. Step 3: Implementacja** (regex `\$\$\{|\$\{(\w+)\}`, obsługa escape najpierw). **Step 4: PASS. Step 5: Commit** `feat(scenario): env substitution`

### Task 7: `scenario/loader.py` — wczytanie YAML → `Scenario`

**Files:** Create `guidebot_recorder/scenario/loader.py`; Test `tests/unit/scenario/test_loader.py`

**Interfaces:**
- Consumes: `models.scenario.Scenario`, `scenario.env.substitute_scenario_values`.
- Produces: `load_scenario(path: Path, env: Mapping[str,str]|None=None) -> LoadedScenario` gdzie `LoadedScenario(scenario: Scenario, doc: CommentedMap)` — `doc` to surowy round-trip handle do fazy zapisu. Substytucja `${ENV}` **tylko** przy budowie `Scenario` (nie w `doc`).

- [ ] **Step 1: Test** — wczytuje przykład z §3.2 (bez `cachedAction`), zwraca Scenario z 4 krokami + zachowany `doc`.
- [ ] **Step 2: FAIL. Step 3: Implementacja** — `ruamel.yaml.YAML(typ="rt")`, kopia do czystego dict dla pydantic. **Step 4: PASS. Step 5: Commit** `feat(scenario): YAML loader`

### Task 8: `scenario/roundtrip.py` — wstrzyknięcie `cachedAction` + zapis atomowy

**Files:** Create `guidebot_recorder/scenario/roundtrip.py`; Test `tests/unit/scenario/test_roundtrip.py`

**Interfaces:**
- Consumes: `CommentedMap`, `models.action.CachedAction`.
- Produces: `inject_cached_action(doc: CommentedMap, step_index: int, action: CachedAction) -> None` (mutuje `doc["steps"][i]["cachedAction"]`, serializacja przez `action.model_dump(by_alias=True, exclude_none=True)`); `atomic_write(path: Path, doc: CommentedMap) -> None` (temp w tym samym katalogu → `os.replace`).

- [ ] **Step 1: Test golden-diff** — plik z komentarzem; po inject komentarz i kolejność zachowane, dodany tylko `cachedAction`.

```python
def test_injection_preserves_comments(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text("config: {title: t, viewport: {width: 1, height: 1}, "
                   "tts: {provider: e, voice: v, lang: pl}}\n"
                   "steps:\n  - teach: kliknij X   # ważny komentarz\n")
    # load doc, inject, atomic_write, then assert '# ważny komentarz' still present
    # and 'cachedAction:' now under the step
```

- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(scenario): in-place cachedAction injection + atomic write`

---

## Faza 2B — `resolver/` (równolegle; kandydat na Codex)

### Task 9: `resolver/page_context.py` — kandydaci z accessibility snapshot

**Files:** Create `guidebot_recorder/resolver/__init__.py`, `guidebot_recorder/resolver/page_context.py`; Test `tests/unit/resolver/test_page_context.py`

**Interfaces:**
- Consumes: Playwright `Page`.
- Produces: `@dataclass Candidate(id: str, role: str, name: str, tag: str, bbox: tuple[float,float,float,float], visible: bool, enabled: bool, ancestry: list[tuple[str,str]])`; `async collect_candidates(page: Page, viewport_only: bool = True, limit: int = 200) -> list[Candidate]` (przycina do interaktywnych ról + nagłówków).

- [ ] **Step 1: Test** — na wstrzykniętym HTML (`page.set_content(...)`) zbiera przycisk „Zaloguj" jako kandydata z `role="button"`, `name="Zaloguj"`, `visible=True`.
- [ ] **Step 2: FAIL. Step 3: Implementacja** — `page.locator` po rolach + `evaluate` dla tag/ancestry/bbox; filtr widoczności. **Step 4: PASS. Step 5: Commit** `feat(resolver): page context candidates`

### Task 10: `resolver/identity_capture.py` — `capture_identity`

**Files:** Create `guidebot_recorder/resolver/identity_capture.py`; Test `tests/unit/resolver/test_identity_capture.py`

**Interfaces:**
- Consumes: Playwright `Locator`, `models.identity.Identity`.
- Produces: `async capture_identity(locator: Locator) -> Identity` (tag lower, `data-testid`, znormalizowany `href` absolutny, `ancestry_digest` = sha256 z listy `(tag,role)` przodków).

- [ ] **Step 1: Test** — dla `<a href="/x" data-testid="lnk">` zwraca `tag="a"`, `testid="lnk"`, `href` absolutny, stabilny `ancestry_digest`.
- [ ] **Step 2: FAIL. Step 3: Implementacja** (`evaluate` po stronie DOM). **Step 4: PASS. Step 5: Commit** `feat(resolver): identity capture`

### Task 11: `resolver/reasoner.py` — `Reasoner` + `CodexReasoner`

**Files:** Create `guidebot_recorder/resolver/reasoner.py`; Test `tests/unit/resolver/test_reasoner.py`

**Interfaces:**
- Consumes: `Candidate`, `models.target.Target`.
- Produces:
  - `@dataclass ReasonerResult(action: Literal["click","hover","type","waitFor"], target: Target)` **lub** `ReasonerError(reason: Literal["no_action","multiple_actions","no_handle"], message: str)`.
  - `class Reasoner(Protocol): async def resolve(self, instruction: str, candidates: list[Candidate]) -> ReasonerResult | ReasonerError`
  - `class CodexReasoner(Reasoner)` — buduje **zredagowany** prompt (bez wartości pól), woła `codex exec` z **framed JSON** (`<<<GUIDEBOT_JSON>>> ... <<<END>>>`), timeout, max 2 próby; brak Codex CLI → `RuntimeError` z instrukcją instalacji.

- [ ] **Step 1: Test z zamockowanym subprocess** — monkeypatch funkcji `_run_codex` zwracającej framed JSON; asercja poprawnego `ReasonerResult(action="click", RoleTarget(...))`. Test błędu: JSON z `"error":"no_action"` → `ReasonerError`.
- [ ] **Step 2: FAIL. Step 3: Implementacja** — `_run_codex(prompt) -> str` (subprocess), `_parse_framed(str) -> dict`, walidacja przez `TypeAdapter(Target)`. **Step 4: PASS. Step 5: Commit** `feat(resolver): Codex reasoner with framed JSON`

### Task 12: `resolver/validate.py` — walidacja compile-time

**Files:** Create `guidebot_recorder/resolver/validate.py`; Test `tests/unit/resolver/test_validate.py`

**Interfaces:**
- Consumes: `Page`, `Target`, `ActionKind`, `Identity`.
- Produces: `async build_locator(page: Page, target: Target) -> Locator` (buduje z pól: `get_by_role`/`get_by_text`/`get_by_label`/`get_by_test_id`, `exact`, `nth`, `scope`→zawężenie); `async validate_compile_time(page, target, action) -> ValidationOk | ValidationFail` (1 trafienie, widoczność, enabled/editable wg akcji, typ zgodny); `async reuse_is_valid(page, cached: CachedAction) -> bool` (jak wyżej **plus** `Identity.matches`).

- [ ] **Step 1: Test** — na HTML z jednym „Zaloguj" `validate_compile_time` OK; z dwoma (substring) bez `exact` → fail (unikalność). `reuse_is_valid` False gdy tożsamość inna.
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(resolver): compile-time validation + locator builder`

---

## Faza 2C — `overlay/` (równolegle; kandydat na Codex)

### Task 13: `overlay/cursor.js` + `overlay/overlay.py`

**Files:** Create `guidebot_recorder/overlay/__init__.py`, `guidebot_recorder/overlay/cursor.js`, `guidebot_recorder/overlay/overlay.py`; Test `tests/unit/overlay/test_overlay.py`

**Interfaces:**
- Consumes: Playwright `Page`.
- Produces:
  - `cursor.js` — definiuje `window.__guidebot_cursor` z metodami `ensure()`, `moveTo(x,y,ms)`, `ripple()`, `highlight(x,y,w,h)`; kursor `position:fixed; pointer-events:none; z-index:2147483647`.
  - `class Overlay`: `async install(page)` (rejestruje `add_init_script(cursor.js)` + wstrzykuje do bieżącego dokumentu), `async ensure(page)` (re-inject gdy brak — rerender SPA), `async move_to(page, x, y, ms=600)`, `async ripple(page)`, `pos: tuple[float,float]` (stan pozycji po stronie Pythona).

- [ ] **Step 1: Test** — po `install` na pustej stronie `page.evaluate("!!window.__guidebot_cursor")` == True; po `move_to(100,100)` element kursora ma left≈100; `ensure` po `set_content` (zniszczenie DOM) ponownie wstrzykuje.
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(overlay): synthetic cursor + re-inject`

---

## Faza 2D — `tts/` (równolegle)

### Task 14: `tts/base.py` — provider protocol + cache + klucz

**Files:** Create `guidebot_recorder/tts/__init__.py`, `guidebot_recorder/tts/base.py`; Test `tests/unit/tts/test_base.py`

**Interfaces:**
- Consumes: `models.config.TtsConfig`.
- Produces:
  - `@dataclass Segment(text: str, path: Path, duration: float)`
  - `class TtsProvider(Protocol): async def synth(self, text: str, tts: TtsConfig, out: Path) -> float` (zwraca długość s)
  - `cache_key(text: str, tts: TtsConfig, adapter_version: int, cache_schema_version: int) -> str` (sha256)
  - `class TtsCache(dir: Path)`: `async get_or_synth(text, tts, provider) -> Segment` (hit z dysku bez syntezowania; miss → `provider.synth` + probe długości + zapis meta).

- [ ] **Step 1: Test z fake providerem** — `synth` tworzy plik i zwraca 1.0; drugie `get_or_synth` **nie** woła providera (hit). Zmiana `voice` → inny klucz (miss).
- [ ] **Step 2: FAIL. Step 3: Implementacja** — długość z metadanych (fake: zapisany json obok) — realny probe w Task 15. **Step 4: PASS. Step 5: Commit** `feat(tts): provider protocol + cache`

### Task 15: `tts/edge.py` — `EdgeTtsProvider`

**Files:** Create `guidebot_recorder/tts/edge.py`; Test `tests/unit/tts/test_edge.py` (oznaczony `@pytest.mark.network`, domyślnie skip w CI)

**Interfaces:**
- Consumes: `TtsProvider`, `edge_tts`.
- Produces: `class EdgeTtsProvider`: `async synth(text, tts, out) -> float` (używa `edge_tts.Communicate(text, voice=tts.voice)`, zapis mp3, długość przez ffprobe).

- [ ] **Step 1: Test** (network, opt-in) — generuje niepusty plik, długość > 0. **Step 2–5:** implementacja + commit `feat(tts): edge-tts provider`

---

## Faza 2E — `video/` (równolegle)

### Task 16: `video/mux.py` — `probe_duration` + `mux`

**Files:** Create `guidebot_recorder/video/__init__.py`, `guidebot_recorder/video/mux.py`; Test `tests/unit/video/test_mux.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Produces: `probe_duration(path: Path) -> float` (ffprobe); `mux(video: Path, audio: Path, out: Path) -> None` (ffmpeg: kopiuje wideo, dodaje audio, `-shortest`, jawny sample rate 48000, kodek aac).

- [ ] **Step 1: Test** — wygeneruj testowy WebM ffmpegiem (`testsrc`), `probe_duration` ≈ zadanej; `mux` z ciszą → plik z 1 ścieżką audio (sprawdź `ffprobe -show_streams`).
- [ ] **Step 2: FAIL. Step 3: Implementacja (subprocess ffmpeg/ffprobe). Step 4: PASS. Step 5: Commit** `feat(video): ffprobe duration + ffmpeg mux`

### Task 17: `video/audiobed.py` — budowa audio bed

**Files:** Create `guidebot_recorder/video/audiobed.py`; Test `tests/unit/video/test_audiobed.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Consumes: `tts.base.Segment`, `probe_duration`.
- Produces: `@dataclass Placed(segment: Segment, offset: float)`; `build_audio_bed(placed: list[Placed], total: float, out: Path) -> None` (ffmpeg `adelay` + `amix`/concat z ciszą do `total`; sample rate 48000).

- [ ] **Step 1: Test** — dwa segmenty na offsetach 0.0 i 2.0, total=4.0 → wynik audio o długości ≈4.0. **Step 2–5:** implementacja + commit `feat(video): silence-padded audio bed`

---

## Faza 3 — Integracja: `recorder/`

### Task 18: `recorder/recorder.py` — `Recorder` API + readiness

**Files:** Create `guidebot_recorder/recorder/__init__.py`, `guidebot_recorder/recorder/recorder.py`; Test `tests/unit/recorder/test_recorder.py`

**Interfaces:**
- Consumes: `Page`, `Overlay`, `validate.build_locator`, `models.action.Expect`.
- Produces: `class Recorder(page, overlay)`: `async navigate(url)`, `async click(target)`, `async hover(target)`, `async enter_text(target, text)`, `async wait_seconds(s)`, `async wait_for(target, state, timeout)`, `async apply_readiness(expect: Expect)`; ruch overlay przed akcją. Metody akcji przyjmują **strukturalny `Target`** (nie tekst).
- Produces (Python API v1): tylko jawne namiary (`recorder.click(RoleTarget(role="button", name="Zaloguj"))`).

- [ ] **Step 1: Test** — na HTML: `navigate` (data URL) → `click(RoleTarget)` klika (asercja efektu, np. zmiana tekstu przez onclick); `apply_readiness("idle")` nie rzuca. Overlay przesuwa się do elementu (pozycja ≈ bbox center).
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(recorder): Recorder API + readiness`

### Task 19: `recorder/compile.py` — algorytm `compile` (§5.6)

**Files:** Create `guidebot_recorder/recorder/compile.py`; Test `tests/unit/recorder/test_compile.py`

**Interfaces:**
- Consumes: `LoadedScenario`, `Reasoner`, `page_context`, `validate`, `identity_capture`, `roundtrip`, `config.config_hash`, `heuristic_expect`.
- Produces: `async run_compile(path: Path, page: Page, reasoner: Reasoner, env) -> None` — pętla §5.6: `say` no-op, `wait:N` pauza, `navigate` goto, kroki z namiarem: reuse jeśli `reuse_is_valid`, inaczej resolve→validate(max 2)→capture identity→`inject_cached_action`+`atomic_write`; po akcji `heuristic_expect` (porównanie URL/networkidle) i zapis `expect`. Wykonuje akcję Playwrightem, `apply_readiness`.
- Produces: `heuristic_expect(url_before, url_after) -> Expect`.

- [ ] **Step 1: Test z zamockowanym Reasonerem** — statyczny HTML z „Zaloguj"; po `run_compile` plik ma `cachedAction` z `strategy: role, name: Zaloguj`, `identity.tag == "button"`, `fingerprint.config_hash` ustawiony. Drugie `run_compile` **nie woła** reasonera (reuse). Zmiana instrukcji → re-resolve.
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(recorder): compile algorithm`

### Task 20: `recorder/render.py` — render + Faza 0 audio + mux

**Files:** Create `guidebot_recorder/recorder/render.py`; Test `tests/unit/recorder/test_render.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Consumes: `LoadedScenario`, `TtsCache`+provider, `Overlay`, `Recorder`, `build_audio_bed`, `mux`, `probe_duration`, Playwright.
- Produces: `async run_render(path, out_mp4, tts_provider, cache_dir, browser) -> None` — Faza 0: pre-synteza całej narracji (`TtsCache`); walidacja braków (twardy błąd gdy TTS pada). Otwarcie kontekstu z `record_video_dir` + viewport z config. Pętla renderu (§9): dla każdego kroku start audio segmentu (offset = monotonic od pierwszej klatki), czekaj `T`, `build_locator`, walidacja render-time (`reuse`/identity — brak/niezgodność → twardy błąd „re-compile"), overlay move+ripple, akcja, readiness. Po zamknięciu: `probe_duration`, `build_audio_bed`, `mux` → `out_mp4`.
- Produces: brak `cachedAction` przy kroku z namiarem lub niezgodna tożsamość → `RenderError("re-compile")`.

- [ ] **Step 1: Test (ffmpeg+chromium)** — na skompilowanym fixture: `run_render` tworzy `out.mp4`; asercje: plik istnieje, `probe_duration` > 0, `ffprobe` pokazuje 1 audio + 1 video; render bez `cachedAction` → `RenderError`.
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(recorder): deterministic render + audio mux`

---

## Faza 4 — CLI + E2E

### Task 21: `cli.py` — `compile` / `render` / `validate`

**Files:** Create `guidebot_recorder/cli.py`; Modify `pyproject.toml` (`[project.scripts] guidebot = "guidebot_recorder.cli:app"`); Test `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `run_compile`, `run_render`, `load_scenario`.
- Produces: typer `app` z komendami `compile PATH`, `render PATH [--out]`, `validate PATH` (samo wczytanie+walidacja schematu); brak Codex CLI / brak zmiennej env / brak cachedAction → czytelny kod wyjścia ≠ 0.

- [ ] **Step 1: Test** (`typer.testing.CliRunner`) — `validate` na dobrym pliku → exit 0; na pliku z dwiema komendami w kroku → exit ≠ 0 z komunikatem.
- [ ] **Step 2: FAIL. Step 3: Implementacja. Step 4: PASS. Step 5: Commit** `feat(cli): compile/render/validate commands`

### Task 22: E2E integracyjny (statyczny HTML)

**Files:** Create `tests/integration/fixtures/app.html`, `tests/integration/fixtures/login.scenario.yaml`, `tests/integration/test_compile_render.py` (`@pytest.mark.integration`)

**Interfaces:**
- Consumes: całość, z **zamockowanym Reasonerem** (fixture zwraca deterministyczne Target dla instrukcji).

- [ ] **Step 1: Test** — serwuj `app.html` (login form), scenariusz `navigate + teach(Zaloguj) + enterText`; `run_compile` (mock reasoner) wypełnia `cachedAction`; ponowny `compile` = brak wywołań reasonera; `run_render` → `out.mp4` z audio (fake TTS provider) i długością > 0. Asercje mocne: ślad akcji (kliknięto element o oczekiwanej tożsamości), 1 ścieżka audio, powtórny render deterministycznie tworzy plik.
- [ ] **Step 2: FAIL. Step 3: Implementacja fixture + testu. Step 4: PASS. Step 5: Commit** `test: end-to-end compile+render on static fixture`

---

## Self-Review (pokrycie specu)

- §2 dwie fazy → Task 19 (compile), 20 (render). §2 render-time identity → Task 12 `reuse_is_valid`, 20.
- §3.1 config/viewport/locale → Task 4. §3.2 komendy + „jedna komenda" + `${ENV}` → Task 5, 6. §3.3 `teach` ograniczenia (0/>1 akcji) → `ReasonerError` Task 11. §3.4 `wait` → Task 5 (`WaitUntil`), 11/12/20 (`waitFor`).
- §4.1 fingerprint (+configHash) → Task 3, 4, 19. §4.2 unia + identity → Task 1,2,3. §4.3 model normatywny → Faza 1. §4 round-trip atomowy → Task 8.
- §5.1 kandydaci → Task 9. §5.2 Reasoner unia → Task 11. §5.3 kontrakt codex (framed/timeout/redakcja) → Task 11. §5.4 walidacja (2 poziomy) → Task 12, 20. §5.5 LLM tylko dane → Task 11/19. §5.6 algorytm → Task 19.
- §6 Recorder + Python API (jawne namiary) → Task 18. §7 overlay (re-inject/pointer-events/scroll) → Task 13, 18. §7.1 readiness/expect → Task 18, 19.
- §8 pre-cache TTS + audio bed + zegar → Task 14,15,17,20. §9 przepływ kroku → Task 20.
- §11 fail-loud → rozproszone (env, reuse, render, cli). §12 testy → każdy task + Task 22. §13 stack/codex dep → Task 0, 11.
- Odłożone (§14): `record`, CDP-attach, `--auto-heal` (błąd „not implemented" w CLI — dodać w Task 21), multi-tab, post-sync. **Uzupełnić w Task 21: `--auto-heal` → NotImplementedError.**

Pozostała precyzja (§17 v4): dokładna normalizacja `href`/`testid`, wejścia `ancestryDigest`, pełna gramatyka — realizowana konkretnie w Task 2, 10, 5 (pydantic + testy).
