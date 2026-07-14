# Guidebot-recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tool that compiles a YAML scenario (human-level intents) into frozen actions, and then deterministically renders a training video `.mp4` with a cursor and TTS narration.

**Architecture:** A two-phase compiler. `compile` launches Playwright, calls the Reasoner (LLM/Codex) only for steps without a valid locator, and writes `cachedAction` in place into the same YAML. `render` reads the frozen actions, replays them without an LLM, records a Playwright video plus an injected cursor overlay, and mixes the TTS narration (pre-cached) with ffmpeg. All modules refer to a single normative data model (`models/`, §4.3 of the spec).

**Tech Stack:** Python 3.12+, uv, Playwright (Python), pydantic v2, ruamel.yaml, typer, edge-tts (default TTS provider, no key required), ffmpeg/ffprobe, pytest + pytest-asyncio.

## Global Constraints

- Python **3.12+**; dependency management via **uv** (`pyproject.toml`).
- All data types are **a single pydantic v2 model** in `guidebot_recorder/models/` — other modules import from here, they do not redefine.
- **`render` does not invoke an LLM/AI** (0×LLM). The LLM is used only in `compile`.
- **Determinism:** the locator is built exclusively from structural fields (no `eval`/parsing of expression strings).
- **Fail-loud:** no silent fallbacks; a missing `${ENV_VAR}` variable, a missing `cachedAction`, a mismatched identity, or a TTS failure → hard error.
- **`${ENV_VAR}`** is expanded only in `enterText.text` and `navigate`; forbidden in `say`/`teach`/`enterText.into`/`wait.until`; escape with `$${`.
- **Closed schema:** unknown keys in `config`/a step → validation error (`model_config = ConfigDict(extra="forbid")`).
- **TDD:** each task = test-first, frequent commits. LLM/network tests are **always mocked** in CI.
- Commit style: `feat:`/`test:`/`chore:` + module scope.

---

## File Structure

```
guidebot_recorder/
  __init__.py
  models/            # §4.3 normative data model — FOUNDATION (Phase 1)
    __init__.py
    target.py        # Target (union discriminated by strategy), Scope
    identity.py      # Identity + equality
    action.py        # CachedAction, Fingerprint, Expect, WaitState, ActionKind
    config.py        # Config, Viewport, TtsConfig, config_hash()
    scenario.py      # Scenario, Step ("single command" validator), commands
  scenario/          # YAML I/O (Phase 2A)
    __init__.py
    env.py           # substitute_env() — value fields + escape
    loader.py        # load_scenario() → (Scenario, CommentedMap)
    roundtrip.py     # inject_cached_action(), atomic_write()
  resolver/          # LLM layer (Phase 2B — Codex candidate)
    __init__.py
    page_context.py  # Candidate, collect_candidates()
    identity_capture.py  # capture_identity() from ElementHandle
    reasoner.py      # Reasoner (Protocol), CodexReasoner, ReasonerResult
    validate.py      # validate_compile_time()
  overlay/           # cursor (Phase 2C — Codex candidate)
    __init__.py
    cursor.js        # injected JS
    overlay.py       # Overlay: install(), ensure(), move_to(), ripple()
  tts/               # narration (Phase 2D)
    __init__.py
    base.py          # TtsProvider (Protocol), Segment, TtsCache, cache_key()
    edge.py          # EdgeTtsProvider (default)
  video/             # recording + mux (Phase 2E)
    __init__.py
    audiobed.py      # build_audio_bed()
    mux.py           # probe_duration(), mux()
  recorder/          # integration (Phase 3)
    __init__.py
    recorder.py      # Recorder (Python API) + readiness
    compile.py       # run_compile()
    render.py        # run_render()
  cli.py             # typer: compile / render / validate  (Phase 4)
tests/
  unit/...           # per module
  integration/
    fixtures/app.html
    test_compile_render.py
pyproject.toml
```

**Dependency graph (what blocks what):**
```
Phase0 scaffold → Phase1 models → ┬─ 2A scenario ─┐
                                 ├─ 2B resolver ─┤
                                 ├─ 2C overlay ──┼→ Phase3 recorder/compile/render → Phase4 cli + e2e
                                 ├─ 2D tts ──────┤
                                 └─ 2E video ────┘
```
Phases **2A–2E are mutually independent** (disjoint directories) → parallelizable. 2B and 2C are good Codex candidates (self-contained, well-defined input/output).

---

## Phase 0 — Scaffolding (sequential, foundation)

### Task 0: uv project + package skeleton + pytest

**Files:**
- Create: `pyproject.toml`, `guidebot_recorder/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`
- Create: `README.md`

**Interfaces:**
- Produces: an installable `guidebot_recorder` package, working `pytest`.

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

- [ ] **Step 2: empty package + smoke test**

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

- [ ] **Step 3: install + run**

Run: `uv sync && uv run playwright install chromium && uv run pytest -q`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore: scaffold uv project + pytest"
```

---

## Phase 1 — Data model (sequential, FOUNDATION §4.3)

> This is the contract consumed by all modules. It must be ready and stable before phase 2.

### Task 1: `models/target.py` — the `Target` union

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

- [ ] **Step 1: Test discrimination and recursive `scope`**

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

- [ ] **Step 2: Run — FAIL** (`uv run pytest tests/unit/models/test_target.py -q`) — the module does not exist.

- [ ] **Step 3: Implementation**

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

### Task 2: `models/identity.py` — `Identity` + equality

**Files:** Create `guidebot_recorder/models/identity.py`; Test `tests/unit/models/test_identity.py`

**Interfaces:**
- Produces: `Identity(tag: str, testid: str | None = None, href: str | None = None, ancestry_digest: str, identity_version: int = 1)`; method `matches(other: Identity) -> bool` (all present fields equal **and** `identity_version` equal).

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

- [ ] **Step 2: FAIL. Step 3: Implementation**

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

### Task 3: `models/action.py` — `CachedAction`, `Fingerprint`, enums

**Files:** Create `guidebot_recorder/models/action.py`; Test `tests/unit/models/test_action.py`

**Interfaces:**
- Produces:
  - `ActionKind = Literal["click","hover","type","waitFor"]`
  - `Expect = Literal["navigation","idle","none"]`
  - `WaitState = Literal["visible","hidden","enabled"]`
  - `Fingerprint(command_kind: str, compiled_from: str, expect: Expect, compiler_version: int, config_hash: str, state: WaitState | None = None)`
  - `CachedAction(action: ActionKind, target: Target, identity: Identity | None, expect: Expect, fingerprint: Fingerprint, state: WaitState | None = None)` (identity optional — `waitFor:hidden` has none)

- [ ] **Step 1: Test** — constructing a click with Target+Identity; waitFor with `state` but no identity.

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

- [ ] **Step 2: FAIL. Step 3: Implementation** (BaseModel `extra="forbid"`, `COMPILER_VERSION = 1` module-level constant). **Step 4: PASS. Step 5: Commit** `feat(models): CachedAction + Fingerprint`

### Task 4: `models/config.py` — `Config` + `config_hash()`

**Files:** Create `guidebot_recorder/models/config.py`; Test `tests/unit/models/test_config.py`

**Interfaces:**
- Produces: `Viewport(width:int,height:int)`, `TtsConfig(provider:str, voice:str, lang:str, model:str|None=None, speed:float|None=None)`, `Config(title:str, viewport:Viewport, tts:TtsConfig, base_url:str|None=None, locale:str|None=None)`, function `config_hash(cfg: Config) -> str` (SHA-256 of a canonical projection: `viewport.width/height`, `locale`, `tts.lang`; `CONFIG_HASH_VERSION=1` in the salt).

- [ ] **Step 1: Test** — hash stability and sensitivity.

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

- [ ] **Step 2: FAIL. Step 3: Implementation** — projection to a dict with sorted keys, `json.dumps(sort_keys=True)`, `hashlib.sha256`. **Step 4: PASS. Step 5: Commit** `feat(models): Config + config_hash`

### Task 5: `models/scenario.py` — `Scenario`, `Step`, "single command" validator

**Files:** Create `guidebot_recorder/models/scenario.py`; Test `tests/unit/models/test_scenario.py`

**Interfaces:**
- Produces:
  - `EnterText(into: str, text: str)`
  - `WaitUntil(until: str, state: WaitState = "visible", timeout: float = 10.0)`
  - `Step(say: str|None=None, teach: str|None=None, enter_text: EnterText|None=None (alias "enterText"), navigate: str|None=None, click: str|None=None, hover: str|None=None, wait: float|WaitUntil|None=None, expect: Expect|None=None, cached_action: CachedAction|None=None (alias "cachedAction"))` — model validator: **exactly one** of the commands {say, teach, enterText, navigate, click, hover, wait}; `say` may accompany an action (`enterText/click/hover`).
  - `Scenario(config: Config, steps: list[Step])`
  - `Step.command_kind() -> str` and `Step.requires_target() -> bool`.

- [ ] **Step 1: Test** — a valid scenario; error on two commands; `say` alongside `enterText` OK.

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

- [ ] **Step 2: FAIL. Step 3: Implementation** — `@model_validator(mode="after")` counts non-None commands (`say` counted only when it is the only one). `requires_target()` = command_kind in {teach, enterText, click, hover} or (wait and wait is a WaitUntil). **Step 4: PASS. Step 5: Commit** `feat(models): Scenario + Step validation`

---

## Phase 2A — `scenario/` YAML I/O (parallel)

### Task 6: `scenario/env.py` — `${ENV_VAR}` substitution

**Files:** Create `guidebot_recorder/scenario/__init__.py`, `guidebot_recorder/scenario/env.py`; Test `tests/unit/scenario/test_env.py`

**Interfaces:**
- Consumes: —
- Produces: `substitute_env(value: str, env: Mapping[str,str]) -> str` (replaces `${VAR}`, escape `$${` → literal `${`, missing variable → `KeyError`); `substitute_scenario_values(raw: dict, env) -> dict` — applies only to `enterText.text` and `navigate`.

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

- [ ] **Step 2: FAIL. Step 3: Implementation** (regex `\$\$\{|\$\{(\w+)\}`, handle the escape first). **Step 4: PASS. Step 5: Commit** `feat(scenario): env substitution`

### Task 7: `scenario/loader.py` — load YAML → `Scenario`

**Files:** Create `guidebot_recorder/scenario/loader.py`; Test `tests/unit/scenario/test_loader.py`

**Interfaces:**
- Consumes: `models.scenario.Scenario`, `scenario.env.substitute_scenario_values`.
- Produces: `load_scenario(path: Path, env: Mapping[str,str]|None=None) -> LoadedScenario` where `LoadedScenario(scenario: Scenario, doc: CommentedMap)` — `doc` is the raw round-trip handle for the write phase. `${ENV}` substitution **only** when building `Scenario` (not in `doc`).

- [ ] **Step 1: Test** — loads the example from §3.2 (without `cachedAction`), returns a Scenario with 4 steps + a preserved `doc`.
- [ ] **Step 2: FAIL. Step 3: Implementation** — `ruamel.yaml.YAML(typ="rt")`, copy to a clean dict for pydantic. **Step 4: PASS. Step 5: Commit** `feat(scenario): YAML loader`

### Task 8: `scenario/roundtrip.py` — inject `cachedAction` + atomic write

**Files:** Create `guidebot_recorder/scenario/roundtrip.py`; Test `tests/unit/scenario/test_roundtrip.py`

**Interfaces:**
- Consumes: `CommentedMap`, `models.action.CachedAction`.
- Produces: `inject_cached_action(doc: CommentedMap, step_index: int, action: CachedAction) -> None` (mutates `doc["steps"][i]["cachedAction"]`, serialization via `action.model_dump(by_alias=True, exclude_none=True)`); `atomic_write(path: Path, doc: CommentedMap) -> None` (temp file in the same directory → `os.replace`).

- [ ] **Step 1: Golden-diff test** — a file with a comment; after inject the comment and ordering are preserved, only `cachedAction` is added.

```python
def test_injection_preserves_comments(tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text("config: {title: t, viewport: {width: 1, height: 1}, "
                   "tts: {provider: e, voice: v, lang: pl}}\n"
                   "steps:\n  - teach: kliknij X   # ważny komentarz\n")
    # load doc, inject, atomic_write, then assert '# ważny komentarz' still present
    # and 'cachedAction:' now under the step
```

- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(scenario): in-place cachedAction injection + atomic write`

---

## Phase 2B — `resolver/` (parallel; Codex candidate)

### Task 9: `resolver/page_context.py` — candidates from the accessibility snapshot

**Files:** Create `guidebot_recorder/resolver/__init__.py`, `guidebot_recorder/resolver/page_context.py`; Test `tests/unit/resolver/test_page_context.py`

**Interfaces:**
- Consumes: Playwright `Page`.
- Produces: `@dataclass Candidate(id: str, role: str, name: str, tag: str, bbox: tuple[float,float,float,float], visible: bool, enabled: bool, ancestry: list[tuple[str,str]])`; `async collect_candidates(page: Page, viewport_only: bool = True, limit: int = 200) -> list[Candidate]` (trims to interactive roles + headings).

- [ ] **Step 1: Test** — on injected HTML (`page.set_content(...)`) collects the "Zaloguj" button as a candidate with `role="button"`, `name="Zaloguj"`, `visible=True`.
- [ ] **Step 2: FAIL. Step 3: Implementation** — `page.locator` by roles + `evaluate` for tag/ancestry/bbox; visibility filter. **Step 4: PASS. Step 5: Commit** `feat(resolver): page context candidates`

### Task 10: `resolver/identity_capture.py` — `capture_identity`

**Files:** Create `guidebot_recorder/resolver/identity_capture.py`; Test `tests/unit/resolver/test_identity_capture.py`

**Interfaces:**
- Consumes: Playwright `Locator`, `models.identity.Identity`.
- Produces: `async capture_identity(locator: Locator) -> Identity` (tag lowercased, `data-testid`, normalized absolute `href`, `ancestry_digest` = sha256 of the list of ancestor `(tag,role)` pairs).

- [ ] **Step 1: Test** — for `<a href="/x" data-testid="lnk">` returns `tag="a"`, `testid="lnk"`, an absolute `href`, and a stable `ancestry_digest`.
- [ ] **Step 2: FAIL. Step 3: Implementation** (`evaluate` on the DOM side). **Step 4: PASS. Step 5: Commit** `feat(resolver): identity capture`

### Task 11: `resolver/reasoner.py` — `Reasoner` + `CodexReasoner`

**Files:** Create `guidebot_recorder/resolver/reasoner.py`; Test `tests/unit/resolver/test_reasoner.py`

**Interfaces:**
- Consumes: `Candidate`, `models.target.Target`.
- Produces:
  - `@dataclass ReasonerResult(action: Literal["click","hover","type","waitFor"], target: Target)` **or** `ReasonerError(reason: Literal["no_action","multiple_actions","no_handle"], message: str)`.
  - `class Reasoner(Protocol): async def resolve(self, instruction: str, candidates: list[Candidate]) -> ReasonerResult | ReasonerError`
  - `class CodexReasoner(Reasoner)` — builds a **redacted** prompt (without field values), calls `codex exec` with **framed JSON** (`<<<GUIDEBOT_JSON>>> ... <<<END>>>`), timeout, max 2 attempts; missing Codex CLI → `RuntimeError` with installation instructions.

- [ ] **Step 1: Test with a mocked subprocess** — monkeypatch the `_run_codex` function to return framed JSON; assert a correct `ReasonerResult(action="click", RoleTarget(...))`. Error test: JSON with `"error":"no_action"` → `ReasonerError`.
- [ ] **Step 2: FAIL. Step 3: Implementation** — `_run_codex(prompt) -> str` (subprocess), `_parse_framed(str) -> dict`, validation via `TypeAdapter(Target)`. **Step 4: PASS. Step 5: Commit** `feat(resolver): Codex reasoner with framed JSON`

### Task 12: `resolver/validate.py` — compile-time validation

**Files:** Create `guidebot_recorder/resolver/validate.py`; Test `tests/unit/resolver/test_validate.py`

**Interfaces:**
- Consumes: `Page`, `Target`, `ActionKind`, `Identity`.
- Produces: `async build_locator(page: Page, target: Target) -> Locator` (builds from fields: `get_by_role`/`get_by_text`/`get_by_label`/`get_by_test_id`, `exact`, `nth`, `scope`→narrowing); `async validate_compile_time(page, target, action) -> ValidationOk | ValidationFail` (1 hit, visibility, enabled/editable per action, type match); `async reuse_is_valid(page, cached: CachedAction) -> bool` (as above **plus** `Identity.matches`).

- [ ] **Step 1: Test** — on HTML with a single "Zaloguj", `validate_compile_time` OK; with two (substring) and no `exact` → fail (uniqueness). `reuse_is_valid` False when the identity differs.
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(resolver): compile-time validation + locator builder`

---

## Phase 2C — `overlay/` (parallel; Codex candidate)

### Task 13: `overlay/cursor.js` + `overlay/overlay.py`

**Files:** Create `guidebot_recorder/overlay/__init__.py`, `guidebot_recorder/overlay/cursor.js`, `guidebot_recorder/overlay/overlay.py`; Test `tests/unit/overlay/test_overlay.py`

**Interfaces:**
- Consumes: Playwright `Page`.
- Produces:
  - `cursor.js` — defines `window.__guidebot_cursor` with methods `ensure()`, `moveTo(x,y,ms)`, `ripple()`, `highlight(x,y,w,h)`; cursor `position:fixed; pointer-events:none; z-index:2147483647`.
  - `class Overlay`: `async install(page)` (registers `add_init_script(cursor.js)` + injects into the current document), `async ensure(page)` (re-inject when absent — SPA rerender), `async move_to(page, x, y, ms=600)`, `async ripple(page)`, `pos: tuple[float,float]` (position state on the Python side).

- [ ] **Step 1: Test** — after `install` on an empty page `page.evaluate("!!window.__guidebot_cursor")` == True; after `move_to(100,100)` the cursor element has left≈100; `ensure` after `set_content` (DOM destruction) re-injects.
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(overlay): synthetic cursor + re-inject`

---

## Phase 2D — `tts/` (parallel)

### Task 14: `tts/base.py` — provider protocol + cache + key

**Files:** Create `guidebot_recorder/tts/__init__.py`, `guidebot_recorder/tts/base.py`; Test `tests/unit/tts/test_base.py`

**Interfaces:**
- Consumes: `models.config.TtsConfig`.
- Produces:
  - `@dataclass Segment(text: str, path: Path, duration: float)`
  - `class TtsProvider(Protocol): async def synth(self, text: str, tts: TtsConfig, out: Path) -> float` (returns length in s)
  - `cache_key(text: str, tts: TtsConfig, adapter_version: int, cache_schema_version: int) -> str` (sha256)
  - `class TtsCache(dir: Path)`: `async get_or_synth(text, tts, provider) -> Segment` (hit from disk without synthesizing; miss → `provider.synth` + length probe + write meta).

- [ ] **Step 1: Test with a fake provider** — `synth` creates a file and returns 1.0; a second `get_or_synth` does **not** call the provider (hit). Changing `voice` → a different key (miss).
- [ ] **Step 2: FAIL. Step 3: Implementation** — length from metadata (fake: a json written alongside) — the real probe is in Task 15. **Step 4: PASS. Step 5: Commit** `feat(tts): provider protocol + cache`

### Task 15: `tts/edge.py` — `EdgeTtsProvider`

**Files:** Create `guidebot_recorder/tts/edge.py`; Test `tests/unit/tts/test_edge.py` (marked `@pytest.mark.network`, skipped by default in CI)

**Interfaces:**
- Consumes: `TtsProvider`, `edge_tts`.
- Produces: `class EdgeTtsProvider`: `async synth(text, tts, out) -> float` (uses `edge_tts.Communicate(text, voice=tts.voice)`, writes mp3, length via ffprobe).

- [ ] **Step 1: Test** (network, opt-in) — generates a non-empty file, length > 0. **Steps 2–5:** implementation + commit `feat(tts): edge-tts provider`

---

## Phase 2E — `video/` (parallel)

### Task 16: `video/mux.py` — `probe_duration` + `mux`

**Files:** Create `guidebot_recorder/video/__init__.py`, `guidebot_recorder/video/mux.py`; Test `tests/unit/video/test_mux.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Produces: `probe_duration(path: Path) -> float` (ffprobe); `mux(video: Path, audio: Path, out: Path) -> None` (ffmpeg: copies the video, adds audio, `-shortest`, explicit sample rate 48000, aac codec).

- [ ] **Step 1: Test** — generate a test WebM with ffmpeg (`testsrc`), `probe_duration` ≈ the given value; `mux` with silence → a file with 1 audio track (check via `ffprobe -show_streams`).
- [ ] **Step 2: FAIL. Step 3: Implementation (ffmpeg/ffprobe subprocess). Step 4: PASS. Step 5: Commit** `feat(video): ffprobe duration + ffmpeg mux`

### Task 17: `video/audiobed.py` — build the audio bed

**Files:** Create `guidebot_recorder/video/audiobed.py`; Test `tests/unit/video/test_audiobed.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Consumes: `tts.base.Segment`, `probe_duration`.
- Produces: `@dataclass Placed(segment: Segment, offset: float)`; `build_audio_bed(placed: list[Placed], total: float, out: Path) -> None` (ffmpeg `adelay` + `amix`/concat with silence up to `total`; sample rate 48000).

- [ ] **Step 1: Test** — two segments at offsets 0.0 and 2.0, total=4.0 → an audio result of length ≈4.0. **Steps 2–5:** implementation + commit `feat(video): silence-padded audio bed`

---

## Phase 3 — Integration: `recorder/`

### Task 18: `recorder/recorder.py` — `Recorder` API + readiness

**Files:** Create `guidebot_recorder/recorder/__init__.py`, `guidebot_recorder/recorder/recorder.py`; Test `tests/unit/recorder/test_recorder.py`

**Interfaces:**
- Consumes: `Page`, `Overlay`, `validate.build_locator`, `models.action.Expect`.
- Produces: `class Recorder(page, overlay)`: `async navigate(url)`, `async click(target)`, `async hover(target)`, `async enter_text(target, text)`, `async wait_seconds(s)`, `async wait_for(target, state, timeout)`, `async apply_readiness(expect: Expect)`; overlay motion before the action. Action methods take a **structural `Target`** (not text).
- Produces (Python API v1): explicit locators only (`recorder.click(RoleTarget(role="button", name="Zaloguj"))`).

- [ ] **Step 1: Test** — on HTML: `navigate` (data URL) → `click(RoleTarget)` clicks (assert the effect, e.g. a text change via onclick); `apply_readiness("idle")` does not raise. The overlay moves to the element (position ≈ bbox center).
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(recorder): Recorder API + readiness`

### Task 19: `recorder/compile.py` — the `compile` algorithm (§5.6)

**Files:** Create `guidebot_recorder/recorder/compile.py`; Test `tests/unit/recorder/test_compile.py`

**Interfaces:**
- Consumes: `LoadedScenario`, `Reasoner`, `page_context`, `validate`, `identity_capture`, `roundtrip`, `config.config_hash`, `heuristic_expect`.
- Produces: `async run_compile(path: Path, page: Page, reasoner: Reasoner, env) -> None` — the §5.6 loop: `say` no-op, `wait:N` pause, `navigate` goto, steps with a locator: reuse if `reuse_is_valid`, otherwise resolve→validate(max 2)→capture identity→`inject_cached_action`+`atomic_write`; after the action `heuristic_expect` (URL/networkidle comparison) and write `expect`. Executes the action with Playwright, `apply_readiness`.
- Produces: `heuristic_expect(url_before, url_after) -> Expect`.

- [ ] **Step 1: Test with a mocked Reasoner** — static HTML with "Zaloguj"; after `run_compile` the file has a `cachedAction` with `strategy: role, name: Zaloguj`, `identity.tag == "button"`, `fingerprint.config_hash` set. A second `run_compile` does **not call** the reasoner (reuse). Changing the instruction → re-resolve.
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(recorder): compile algorithm`

### Task 20: `recorder/render.py` — render + Phase 0 audio + mux

**Files:** Create `guidebot_recorder/recorder/render.py`; Test `tests/unit/recorder/test_render.py` (`@pytest.mark.ffmpeg`)

**Interfaces:**
- Consumes: `LoadedScenario`, `TtsCache`+provider, `Overlay`, `Recorder`, `build_audio_bed`, `mux`, `probe_duration`, Playwright.
- Produces: `async run_render(path, out_mp4, tts_provider, cache_dir, browser) -> None` — Phase 0: pre-synthesize the entire narration (`TtsCache`); validate for gaps (hard error when TTS fails). Open a context with `record_video_dir` + viewport from config. Render loop (§9): for each step start the audio segment (offset = monotonic from the first frame), wait `T`, `build_locator`, render-time validation (`reuse`/identity — missing/mismatch → hard "re-compile" error), overlay move+ripple, action, readiness. After closing: `probe_duration`, `build_audio_bed`, `mux` → `out_mp4`.
- Produces: a missing `cachedAction` on a step with a locator or a mismatched identity → `RenderError("re-compile")`.

- [ ] **Step 1: Test (ffmpeg+chromium)** — on a compiled fixture: `run_render` creates `out.mp4`; assertions: the file exists, `probe_duration` > 0, `ffprobe` shows 1 audio + 1 video; render without `cachedAction` → `RenderError`.
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(recorder): deterministic render + audio mux`

---

## Phase 4 — CLI + E2E

### Task 21: `cli.py` — `compile` / `render` / `validate`

**Files:** Create `guidebot_recorder/cli.py`; Modify `pyproject.toml` (`[project.scripts] guidebot = "guidebot_recorder.cli:app"`); Test `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `run_compile`, `run_render`, `load_scenario`.
- Produces: a typer `app` with commands `compile PATH`, `render PATH [--out]`, `validate PATH` (load + schema validation only); missing Codex CLI / missing env variable / missing cachedAction → a clear exit code ≠ 0.

- [ ] **Step 1: Test** (`typer.testing.CliRunner`) — `validate` on a good file → exit 0; on a file with two commands in a step → exit ≠ 0 with a message.
- [ ] **Step 2: FAIL. Step 3: Implementation. Step 4: PASS. Step 5: Commit** `feat(cli): compile/render/validate commands`

### Task 22: E2E integration (static HTML)

**Files:** Create `tests/integration/fixtures/app.html`, `tests/integration/fixtures/login.scenario.yaml`, `tests/integration/test_compile_render.py` (`@pytest.mark.integration`)

**Interfaces:**
- Consumes: the whole system, with a **mocked Reasoner** (fixture returns a deterministic Target for each instruction).

- [ ] **Step 1: Test** — serve `app.html` (login form), scenario `navigate + teach(Zaloguj) + enterText`; `run_compile` (mock reasoner) fills in `cachedAction`; a repeated `compile` = no reasoner calls; `run_render` → `out.mp4` with audio (fake TTS provider) and length > 0. Strong assertions: an action trace (clicked the element with the expected identity), 1 audio track, a repeated render deterministically creates the file.
- [ ] **Step 2: FAIL. Step 3: Implement the fixture + test. Step 4: PASS. Step 5: Commit** `test: end-to-end compile+render on static fixture`

---

## Self-Review (spec coverage)

- §2 two phases → Task 19 (compile), 20 (render). §2 render-time identity → Task 12 `reuse_is_valid`, 20.
- §3.1 config/viewport/locale → Task 4. §3.2 commands + "single command" + `${ENV}` → Task 5, 6. §3.3 `teach` constraints (0/>1 actions) → `ReasonerError` Task 11. §3.4 `wait` → Task 5 (`WaitUntil`), 11/12/20 (`waitFor`).
- §4.1 fingerprint (+configHash) → Task 3, 4, 19. §4.2 union + identity → Task 1,2,3. §4.3 normative model → Phase 1. §4 atomic round-trip → Task 8.
- §5.1 candidates → Task 9. §5.2 Reasoner union → Task 11. §5.3 codex contract (framed/timeout/redaction) → Task 11. §5.4 validation (2 levels) → Task 12, 20. §5.5 LLM data only → Task 11/19. §5.6 algorithm → Task 19.
- §6 Recorder + Python API (explicit locators) → Task 18. §7 overlay (re-inject/pointer-events/scroll) → Task 13, 18. §7.1 readiness/expect → Task 18, 19.
- §8 TTS pre-cache + audio bed + clock → Task 14,15,17,20. §9 step flow → Task 20.
- §11 fail-loud → distributed (env, reuse, render, cli). §12 tests → every task + Task 22. §13 stack/codex dep → Task 0, 11.
- Deferred (§14): `record`, CDP-attach, `--auto-heal` ("not implemented" error in the CLI — add in Task 21), multi-tab, post-sync. **To complete in Task 21: `--auto-heal` → NotImplementedError.**

Remaining precision (§17 v4): exact normalization of `href`/`testid`, `ancestryDigest` inputs, the full grammar — realized concretely in Task 2, 10, 5 (pydantic + tests).
