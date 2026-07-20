# PDF Step-by-Step Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `guidebot guide` command that turns an already-compiled scenario into a landscape, step-by-step PDF: annotated screenshot left, description right — one page per meaningful step, no LLM, no change to the video renderer.

**Architecture:** A new `guidebot_recorder/guide/` package runs its own Playwright capture pass reusing the existing `Recorder` (a new public `point()` API returns the target geometry), takes one `page.screenshot()` per step, draws annotations as an SVG layer over the image in a composed HTML document, and prints it to PDF via Chromium `page.pdf()`. Control flow mirrors the renderer's step loop (dispatch on `cached.action`, skip absent `when` branches, fail fast on popups).

**Tech Stack:** Python 3.12+, Playwright (async), Pydantic v2, Typer. No new runtime dependencies.

## Global Constraints

- Python `>=3.12`; runtime deps limited to those already in `pyproject.toml` (playwright, pydantic, ruamel.yaml, typer, edge-tts, tqdm). **No new runtime dependency.**
- Renderer (`recorder/render.py`) and its tests must not regress; the only touch to shared code is a pure refactor of `Recorder._point_and_prepare`.
- User-facing CLI text and errors are Polish (match existing `render` command style: `typer.echo("BŁĄD: …", err=True)`).
- ruff line-length 100. Files stay small and single-responsibility.
- Dispatch on `cached.action` (`click`/`hover`/`type`/`waitFor`), never on `step.command_kind()`.
- PDF phase is always headless (`page.pdf()` throws in headed Chromium).
- Reference spec: `docs/superpowers/specs/2026-07-20-pdf-step-guide-design.md`.

---

### Task 1: Add optional `caption` field to `Step`

**Files:**
- Modify: `guidebot_recorder/models/scenario.py` (the `Step` class, after `say`)
- Test: `tests/unit/models/test_scenario.py`

**Interfaces:**
- Produces: `Step.caption: str | None` — richer PDF text that overrides narration.

- [ ] **Step 1: Write the failing test**

```python
def test_step_accepts_optional_caption():
    from guidebot_recorder.models.scenario import Step
    step = Step(click="the login button", caption="Kliknij duży niebieski przycisk „Zaloguj”.")
    assert step.caption == "Kliknij duży niebieski przycisk „Zaloguj”."


def test_step_caption_defaults_to_none():
    from guidebot_recorder.models.scenario import Step
    assert Step(say="hello").caption is None


def test_step_with_only_caption_is_still_empty_step():
    import pytest
    from pydantic import ValidationError
    from guidebot_recorder.models.scenario import Step
    with pytest.raises(ValidationError):
        Step(caption="tekst bez komendy i bez say")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/models/test_scenario.py -k caption -v`
Expected: FAIL — `Step` rejects unknown field `caption` (`extra="forbid"`).

- [ ] **Step 3: Add the field**

In `guidebot_recorder/models/scenario.py`, inside `class Step`, add directly under the `say` field:

```python
    #: richer per-step text for the PDF guide; overrides narration in `guide`,
    #: ignored by the video renderer. Not a command (does not count toward
    #: "exactly one command"); a step with only `caption` is still an empty step.
    caption: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/models/test_scenario.py -k caption -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/models/scenario.py tests/unit/models/test_scenario.py
git commit -m "feat(model): add optional Step.caption for the PDF guide"
```

---

### Task 2: Public `Recorder.point()` API (refactor `_point_and_prepare`)

**Files:**
- Modify: `guidebot_recorder/recorder/recorder.py`
- Test: `tests/unit/recorder/test_recorder.py`

**Interfaces:**
- Produces:
  - `Recorder.PointResult` = `NamedTuple(locator: Locator, box: dict | None, center: tuple[float, float] | None)`
  - `async Recorder.point(target: Target, *, ripple: bool = True, click_sound: bool = False) -> PointResult`
  - `_point_and_prepare` keeps its old signature/behavior but delegates to `point`.

- [ ] **Step 1: Write the failing test**

Uses fakes — no real browser. Add to `tests/unit/recorder/test_recorder.py`:

```python
import pytest
from guidebot_recorder.recorder.recorder import Recorder


class _FakeLocator:
    def __init__(self, box):
        self._box = box
        self.hovered = False
    async def evaluate(self, _script):
        return None
    async def bounding_box(self):
        return self._box


class _FakeOverlay:
    def __init__(self):
        self.moves = []
        self.ripples = 0
    async def move_to(self, _page, x, y):
        self.moves.append((x, y))
    async def ripple(self, _page, flash=False):
        self.ripples += 1


class _FakePage:
    async def wait_for_timeout(self, _ms):
        return None


@pytest.fixture
def patched_locator(monkeypatch):
    box = {"x": 10, "y": 20, "width": 100, "height": 40}
    loc = _FakeLocator(box)
    async def _fake_build_locator(_frame, _target):
        return loc
    monkeypatch.setattr("guidebot_recorder.recorder.recorder.build_locator", _fake_build_locator)
    return loc, box


async def test_point_returns_center_and_box(patched_locator):
    loc, box = patched_locator
    overlay = _FakeOverlay()
    rec = Recorder(_FakePage(), overlay)
    res = await rec.point(object())  # target unused by the fake build_locator
    assert res.locator is loc
    assert res.box == box
    assert res.center == (60.0, 40.0)  # 10+100/2, 20+40/2


async def test_point_ripple_false_skips_ripple(patched_locator):
    overlay = _FakeOverlay()
    rec = Recorder(_FakePage(), overlay)
    await rec.point(object(), ripple=False)
    assert overlay.ripples == 0
    assert overlay.moves == [(60.0, 40.0)]  # still moves the cursor


async def test_point_no_box_gives_none_center(monkeypatch):
    loc = _FakeLocator(None)
    async def _fake_build_locator(_frame, _target):
        return loc
    monkeypatch.setattr("guidebot_recorder.recorder.recorder.build_locator", _fake_build_locator)
    rec = Recorder(_FakePage(), _FakeOverlay())
    res = await rec.point(object())
    assert res.box is None
    assert res.center is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/recorder/test_recorder.py -k point -v`
Expected: FAIL — `Recorder` has no attribute `point`.

- [ ] **Step 3: Implement `point` and rebuild `_point_and_prepare` on it**

In `guidebot_recorder/recorder/recorder.py`, add `NamedTuple` to the imports at top:

```python
from typing import NamedTuple
```

Add the result type just above `class Recorder`:

```python
class PointResult(NamedTuple):
    locator: Locator
    box: dict | None
    center: tuple[float, float] | None
```

Replace the existing `_point_and_prepare` (lines 66-85) with:

```python
    async def point(
        self, target: Target, *, ripple: bool = True, click_sound: bool = False
    ) -> PointResult:
        """Resolve the target, scroll it into view, move the cursor onto it.

        Returns the locator plus the target's bounding box and center (viewport
        pixels) so callers (e.g. the PDF guide) can annotate without re-resolving.
        ``ripple=False`` suppresses the click ring — a still capture wants a
        clean frame. ``box``/``center`` are None when the element has no box.
        """
        locator = await build_locator(self.frame, target)
        await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
        box: dict | None = None
        center: tuple[float, float] | None = None
        rippled = False
        if self.overlay is not None:
            box = await locator.bounding_box()
            if box is not None:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                center = (cx, cy)
                await self.overlay.move_to(self.page, cx, cy)
                if ripple:
                    await self.overlay.ripple(self.page, flash=click_sound)
                    if click_sound and self._on_sfx is not None:
                        self._on_sfx("click")
                    rippled = True
                    await self.page.wait_for_timeout(self.settle_ms)
        if click_sound and not rippled and self._on_sfx is not None:
            self._on_sfx("click")
        return PointResult(locator, box, center)

    async def _point_and_prepare(self, target: Target, *, click_sound: bool = False) -> Locator:
        res = await self.point(target, ripple=True, click_sound=click_sound)
        return res.locator
```

Note: `box`/`center` are only populated when an overlay is present (the guide always builds its `Recorder` with an overlay). This preserves the exact compile-mode behavior (`Recorder(page, None)` still computes no box).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/recorder/test_recorder.py -v`
Expected: PASS (new `point` tests + all pre-existing recorder tests still green).

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/recorder/recorder.py tests/unit/recorder/test_recorder.py
git commit -m "refactor(recorder): expose public point() returning target geometry"
```

---

### Task 3: Guide data model + `page_text`

**Files:**
- Create: `guidebot_recorder/guide/__init__.py` (empty)
- Create: `guidebot_recorder/guide/model.py`
- Create: `tests/unit/guide/__init__.py` (empty)
- Create: `tests/unit/guide/test_model.py`

**Interfaces:**
- Produces:
  - `Annotation` dataclass (kind + optional per-kind coords).
  - `GuidePage` dataclass (`kind`, `screenshot`, `text`, `heading`, `annotations`, `screenshot_size`).
  - `page_text(step: Step) -> str` — `caption` else narration else `""`.

- [ ] **Step 1: Write the failing test**

`tests/unit/guide/test_model.py`:

```python
from guidebot_recorder.guide.model import Annotation, GuidePage, page_text
from guidebot_recorder.models.scenario import Step


def test_page_text_prefers_caption():
    step = Step(click="btn", say="krótko", caption="dłuższy opis do PDF")
    assert page_text(step) == "dłuższy opis do PDF"


def test_page_text_falls_back_to_narration():
    assert page_text(Step(teach="wpisz login")) == "wpisz login"


def test_page_text_empty_when_no_text():
    assert page_text(Step(navigate="https://example.com")) == ""


def test_annotation_and_page_construct():
    a = Annotation(kind="click", cx=1.0, cy=2.0, r=18.0)
    page = GuidePage(
        kind="step", screenshot=None, text="t", heading=None,
        annotations=[a], screenshot_size=(800, 600),
    )
    assert page.annotations[0].kind == "click"
    assert page.screenshot_size == (800, 600)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/guide/test_model.py -v`
Expected: FAIL — module `guidebot_recorder.guide.model` does not exist.

- [ ] **Step 3: Implement the model**

Create `guidebot_recorder/guide/__init__.py` (empty) and `tests/unit/guide/__init__.py` (empty).

Create `guidebot_recorder/guide/model.py`:

```python
"""Data model for the step-by-step PDF guide (in-memory only, never serialized)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from guidebot_recorder.models.scenario import Step


@dataclass
class Annotation:
    """One overlay mark, in screenshot pixels. Only the fields for `kind` are set."""

    kind: Literal["arrow", "click", "typed", "hover"]
    # arrow: prev cursor -> target center
    x1: float | None = None
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    # click: circle at target center
    cx: float | None = None
    cy: float | None = None
    r: float | None = None
    # typed / hover: rectangle around the target box
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None


@dataclass
class GuidePage:
    """One PDF page: a screenshot (or none) plus its description and annotations."""

    kind: Literal["step", "navigate", "slide", "text"]
    screenshot: Path | None
    text: str
    heading: str | None
    annotations: list[Annotation] = field(default_factory=list)
    screenshot_size: tuple[int, int] | None = None


def page_text(step: Step) -> str:
    """Right-hand description: caption overrides narration; empty if neither."""

    return step.caption or step.narration() or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/guide/test_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/guide/__init__.py guidebot_recorder/guide/model.py tests/unit/guide/__init__.py tests/unit/guide/test_model.py
git commit -m "feat(guide): add GuidePage/Annotation model and page_text"
```

---

### Task 4: Annotation geometry (`annotate.py`)

**Files:**
- Create: `guidebot_recorder/guide/annotate.py`
- Create: `tests/unit/guide/test_annotate.py`

**Interfaces:**
- Consumes: `Annotation` from Task 3; `ActionKind` = `cached.action` string.
- Produces: `annotations_for(action: str, prev_cursor, center, box) -> list[Annotation]`
  - `action` ∈ `{"click","hover","type"}`.
  - `prev_cursor: tuple[float,float] | None`, `center: tuple[float,float] | None`, `box: dict | None`.
  - Rules: arrow when `prev_cursor` and `center` both present; `click`→circle at center; `hover`→glow rect = box; `type`→frame rect = box. Missing geometry → that mark is omitted (never crashes).

- [ ] **Step 1: Write the failing test**

`tests/unit/guide/test_annotate.py`:

```python
from guidebot_recorder.guide.annotate import CLICK_RADIUS, annotations_for


BOX = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}
CENTER = (60.0, 40.0)


def _kinds(anns):
    return [a.kind for a in anns]


def test_click_has_circle_and_arrow_from_prev():
    anns = annotations_for("click", prev_cursor=(5.0, 5.0), center=CENTER, box=BOX)
    assert set(_kinds(anns)) == {"arrow", "click"}
    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x1, arrow.y1, arrow.x2, arrow.y2) == (5.0, 5.0, 60.0, 40.0)
    circle = next(a for a in anns if a.kind == "click")
    assert (circle.cx, circle.cy, circle.r) == (60.0, 40.0, CLICK_RADIUS)


def test_no_arrow_without_prev_cursor():
    anns = annotations_for("click", prev_cursor=None, center=CENTER, box=BOX)
    assert _kinds(anns) == ["click"]


def test_type_makes_typed_frame():
    anns = annotations_for("type", prev_cursor=None, center=CENTER, box=BOX)
    typed = next(a for a in anns if a.kind == "typed")
    assert (typed.x, typed.y, typed.w, typed.h) == (10.0, 20.0, 100.0, 40.0)


def test_hover_makes_glow_rect():
    anns = annotations_for("hover", prev_cursor=None, center=CENTER, box=BOX)
    assert _kinds(anns) == ["hover"]


def test_missing_box_omits_rect_marks():
    anns = annotations_for("type", prev_cursor=None, center=None, box=None)
    assert anns == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/guide/test_annotate.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement geometry**

Create `guidebot_recorder/guide/annotate.py`:

```python
"""Pure annotation geometry — no I/O, no browser. Coordinates are screenshot pixels."""

from __future__ import annotations

from guidebot_recorder.guide.model import Annotation

#: fixed click-circle radius (screenshot px, deviceScaleFactor already applied upstream)
CLICK_RADIUS = 22.0

_Point = tuple[float, float]


def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    center: _Point | None,
    box: dict | None,
) -> list[Annotation]:
    """Build the marks for one target action, omitting any mark that lacks geometry."""

    anns: list[Annotation] = []
    if prev_cursor is not None and center is not None:
        anns.append(
            Annotation(kind="arrow", x1=prev_cursor[0], y1=prev_cursor[1], x2=center[0], y2=center[1])
        )
    if action == "click" and center is not None:
        anns.append(Annotation(kind="click", cx=center[0], cy=center[1], r=CLICK_RADIUS))
    elif action == "hover" and box is not None:
        anns.append(Annotation(kind="hover", x=box["x"], y=box["y"], w=box["width"], h=box["height"]))
    elif action == "type" and box is not None:
        anns.append(Annotation(kind="typed", x=box["x"], y=box["y"], w=box["width"], h=box["height"]))
    return anns
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/guide/test_annotate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/guide/annotate.py tests/unit/guide/test_annotate.py
git commit -m "feat(guide): pure annotation geometry (arrow/click/typed/hover)"
```

---

### Task 5: Static prolog + step classification (`prolog.py`)

**Files:**
- Create: `guidebot_recorder/guide/prolog.py`
- Create: `tests/unit/guide/test_prolog.py`

**Interfaces:**
- Consumes: `Scenario.flat_steps()` → `list[FlatStep]`; `CompiledScenario.actions` → `list[CompiledAction | None]`; `CachedAction` (has `.action`, `.opens_popup`), `PendingAction` (from `models.action`).
- Produces:
  - `class GuideError(Exception)`
  - `scan_for_blockers(flat: list[FlatStep], actions: list) -> None` — raises `GuideError` for a popup action anywhere, or a `PendingAction` on a mandatory non-gate, non-optional step that requires a target.
  - `classify(flat_step: FlatStep) -> Literal["gate","navigate","slide","action","text","wait"]` — page category ignoring runtime skips. `gate`/`wait` → no page; `text` → say-only or wait+say; `action` → click/hover/type/teach with a target.

- [ ] **Step 1: Write the failing test**

`tests/unit/guide/test_prolog.py`:

Constructors follow the existing pattern in `tests/unit/models/test_action.py`
(RoleTarget + a required `Fingerprint`; both `CachedAction` and `PendingAction`
are `extra="forbid"` and require `fingerprint`). `Config` requires `name` +
`base_url` (confirm against `models/config.py` if it FAILs at Step 2).

```python
import pytest
from guidebot_recorder.guide.prolog import GuideError, classify, scan_for_blockers
from guidebot_recorder.models.action import CachedAction, Fingerprint, PendingAction
from guidebot_recorder.models.config import Config
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WhenBlock
from guidebot_recorder.models.target import RoleTarget


def _cfg():
    return Config(name="t", base_url="https://example.com")


def _fp(command_kind="click"):
    return Fingerprint(
        command_kind=command_kind, compiled_from="x", expect="none", config_hash="c"
    )


def _cached(action="click", opens_popup=False):
    return CachedAction(
        action=action,
        target=RoleTarget(role="button", name="x"),
        expect="none",
        opens_popup=opens_popup,
        fingerprint=_fp(),
    )


def _pending():
    return PendingAction(fingerprint=_fp())


def classify_step_of(step):
    return classify(FlatStep(step=step, branch=None, is_gate=False))


def test_classify_kinds():
    assert classify_step_of(Step(navigate="https://x")) == "navigate"
    assert classify_step_of(Step(slide={"title": "Sekcja"})) == "slide"
    assert classify_step_of(Step(click="btn")) == "action"
    assert classify_step_of(Step(say="tylko narracja")) == "text"
    assert classify_step_of(Step(wait=1.5)) == "wait"
    assert classify_step_of(Step(wait=1.5, say="czekamy")) == "text"


def test_scan_raises_on_popup():
    scen = Scenario(config=_cfg(), steps=[Step(click="opens something")])
    with pytest.raises(GuideError, match="popup"):
        scan_for_blockers(scen.flat_steps(), [_cached(opens_popup=True)])


def test_scan_raises_on_mandatory_pending():
    scen = Scenario(config=_cfg(), steps=[Step(click="btn")])
    with pytest.raises(GuideError, match="compile"):
        scan_for_blockers(scen.flat_steps(), [_pending()])


def test_scan_allows_pending_on_optional():
    scen = Scenario(config=_cfg(), steps=[Step(click="btn", optional=True)])
    scan_for_blockers(scen.flat_steps(), [_pending()])  # no raise


def test_scan_allows_pending_on_gate():
    scen = Scenario(config=_cfg(), steps=[WhenBlock(when="a banner", steps=[Step(click="ok")])])
    # gate action pending + child cached is fine (branch may never have compiled)
    scan_for_blockers(scen.flat_steps(), [_pending(), _cached()])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/guide/test_prolog.py -v`
Expected: FAIL — module does not exist. (If `CachedAction`/`PendingAction`/`Target` constructor kwargs differ, read `guidebot_recorder/models/action.py` and `target.py` and fix the test's constructors first — keep the assertions.)

- [ ] **Step 3: Implement prolog**

Create `guidebot_recorder/guide/prolog.py`:

```python
"""Static (no-browser) checks and page classification for the PDF guide."""

from __future__ import annotations

from typing import Literal

from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import FlatStep


class GuideError(Exception):
    """A scenario the guide cannot render (popup, unresolved mandatory step)."""


PageKind = Literal["gate", "navigate", "slide", "action", "text", "wait"]


def classify(flat_step: FlatStep) -> PageKind:
    if flat_step.is_gate:
        return "gate"
    step = flat_step.step
    kind = step.command_kind()
    if kind == "navigate":
        return "navigate"
    if kind == "slide":
        return "slide"
    if kind in ("click", "hover", "enterText", "teach"):
        return "action"
    if kind == "wait":
        return "text" if step.say else "wait"
    # say-only
    return "text" if step.say else "wait"


def scan_for_blockers(flat: list[FlatStep], actions: list) -> None:
    """Raise GuideError for popups anywhere, or a mandatory unresolved step."""

    for flat_step, action in zip(flat, actions, strict=True):
        if isinstance(action, CachedAction) and action.opens_popup:
            raise GuideError(
                "scenariusze z popupem nie są obsługiwane w `guide` v1 "
                "(krok otwiera nowe okno)"
            )
        pending = action is not None and not isinstance(action, CachedAction)
        mandatory = (
            flat_step.branch is None
            and not flat_step.is_gate
            and not flat_step.step.optional
            and flat_step.step.requires_target()
        )
        if pending and mandatory:
            raise GuideError(
                "skompilowany scenariusz ma nierozwiązany krok obowiązkowy — "
                "uruchom `guidebot compile` (lub `compile --force`)"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/guide/test_prolog.py -v`
Expected: PASS. If a constructor kwarg was wrong, the FAIL in Step 2 told you; adjust the test constructors only.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/guide/prolog.py tests/unit/guide/test_prolog.py
git commit -m "feat(guide): static popup/pending prolog and step classification"
```

---

### Task 6: HTML layout (`layout.py`)

**Files:**
- Create: `guidebot_recorder/guide/layout.py`
- Create: `tests/unit/guide/test_layout.py`

**Interfaces:**
- Consumes: `list[GuidePage]`, `Annotation` (Task 3).
- Produces: `render_html(pages: list[GuidePage], *, title: str) -> str` — one `<section class="page">` per page; screenshot pages embed `<img>` with the PNG as a `file://` or `data:` URI plus an `<svg viewBox="0 0 W H">` overlay carrying the annotations; text/slide pages render heading+text only. Landscape sizing via CSS `@page { size: landscape; }`.

- [ ] **Step 1: Write the failing test**

`tests/unit/guide/test_layout.py`:

```python
from pathlib import Path
from guidebot_recorder.guide.layout import render_html
from guidebot_recorder.guide.model import Annotation, GuidePage


def test_one_section_per_page_and_title():
    pages = [
        GuidePage(kind="slide", screenshot=None, text="", heading="Sekcja 1", annotations=[]),
        GuidePage(
            kind="step", screenshot=Path("/tmp/shot.png"), text="Kliknij tu",
            heading=None,
            annotations=[Annotation(kind="click", cx=60.0, cy=40.0, r=22.0)],
            screenshot_size=(800, 600),
        ),
    ]
    html = render_html(pages, title="Mój przewodnik")
    assert html.count('class="page"') == 2
    assert "Mój przewodnik" in html


def test_screenshot_page_has_svg_viewbox_and_circle():
    pages = [GuidePage(
        kind="step", screenshot=Path("/tmp/shot.png"), text="t", heading=None,
        annotations=[Annotation(kind="click", cx=1.0, cy=2.0, r=22.0)],
        screenshot_size=(800, 600),
    )]
    html = render_html(pages, title="x")
    assert 'viewBox="0 0 800 600"' in html
    assert "<circle" in html


def test_text_page_has_no_svg():
    pages = [GuidePage(kind="text", screenshot=None, text="tylko tekst", heading=None, annotations=[])]
    html = render_html(pages, title="x")
    assert "<svg" not in html
    assert "tylko tekst" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/guide/test_layout.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement layout**

Create `guidebot_recorder/guide/layout.py`. Use `html.escape` for all text; embed the screenshot as a `file://` URI (Chromium reads local files when we `set_content` with a `file://` base or navigate to the file — the PDF task loads via a temp HTML file, so `file://` works). Render each annotation kind into SVG.

```python
"""Compose GuidePage list into one landscape HTML document for Chromium page.pdf()."""

from __future__ import annotations

import html
from pathlib import Path

from guidebot_recorder.guide.model import Annotation, GuidePage

_STYLE = """
@page { size: A4 landscape; margin: 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #1a1a1a; margin: 0; }
.page { display: grid; grid-template-columns: 62% 38%; gap: 6mm; height: 100vh;
        page-break-after: always; align-items: start; }
.page:last-child { page-break-after: auto; }
.shot { position: relative; width: 100%; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }
.shot img { width: 100%; display: block; }
.shot svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.side { padding-top: 2mm; }
.side .heading { font-size: 20px; font-weight: 700; margin: 0 0 4mm; }
.side .body { font-size: 16px; line-height: 1.5; white-space: pre-wrap; }
.slide { grid-column: 1 / -1; display: flex; flex-direction: column; justify-content: center;
         height: 100vh; text-align: center; }
.slide .title { font-size: 40px; font-weight: 800; }
.slide .subtitle { font-size: 24px; color: #555; margin-top: 4mm; }
.arrow { stroke: #e11; stroke-width: 4; fill: none; marker-end: url(#ah); }
.circle { stroke: #e11; stroke-width: 4; fill: none; }
.rect { stroke: #e11; stroke-width: 4; fill: rgba(238,17,17,0.08); }
"""

_ARROW_MARKER = (
    '<defs><marker id="ah" markerWidth="10" markerHeight="10" refX="8" refY="5" '
    'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#e11"/></marker></defs>'
)


def _svg(anns: list[Annotation], size: tuple[int, int]) -> str:
    w, h = size
    parts = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">', _ARROW_MARKER]
    for a in anns:
        if a.kind == "arrow":
            parts.append(
                f'<line class="arrow" x1="{a.x1}" y1="{a.y1}" x2="{a.x2}" y2="{a.y2}"/>'
            )
        elif a.kind == "click":
            parts.append(f'<circle class="circle" cx="{a.cx}" cy="{a.cy}" r="{a.r}"/>')
        elif a.kind in ("typed", "hover"):
            parts.append(
                f'<rect class="rect" x="{a.x}" y="{a.y}" width="{a.w}" height="{a.h}" rx="4"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _shot_page(page: GuidePage) -> str:
    uri = Path(page.screenshot).absolute().as_uri()
    svg = _svg(page.annotations, page.screenshot_size or (1, 1))
    heading = f'<div class="heading">{html.escape(page.heading)}</div>' if page.heading else ""
    body = f'<div class="body">{html.escape(page.text)}</div>' if page.text else ""
    return (
        '<section class="page">'
        f'<div class="shot"><img src="{uri}"/>{svg}</div>'
        f'<div class="side">{heading}{body}</div>'
        "</section>"
    )


def _slide_page(page: GuidePage) -> str:
    title = f'<div class="title">{html.escape(page.heading or page.text)}</div>'
    sub = f'<div class="subtitle">{html.escape(page.text)}</div>' if page.heading and page.text else ""
    return f'<section class="page"><div class="slide">{title}{sub}</div></section>'


def _text_page(page: GuidePage) -> str:
    heading = f'<div class="heading">{html.escape(page.heading)}</div>' if page.heading else ""
    return (
        '<section class="page"><div class="side" style="grid-column:1/-1">'
        f'{heading}<div class="body">{html.escape(page.text)}</div></div></section>'
    )


def render_html(pages: list[GuidePage], *, title: str) -> str:
    body_parts: list[str] = []
    for page in pages:
        if page.kind == "slide":
            body_parts.append(_slide_page(page))
        elif page.screenshot is not None:
            body_parts.append(_shot_page(page))
        else:
            body_parts.append(_text_page(page))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/guide/test_layout.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/guide/layout.py tests/unit/guide/test_layout.py
git commit -m "feat(guide): HTML+SVG landscape layout for the PDF"
```

---

### Task 7: PDF renderer (`pdf.py`)

**Files:**
- Create: `guidebot_recorder/guide/pdf.py`
- Create: `tests/unit/guide/test_pdf.py`

**Interfaces:**
- Consumes: an HTML string (Task 6), a Playwright `Browser`.
- Produces: `async html_to_pdf(browser: Browser, html: str, out_pdf: Path) -> None` — writes HTML to a temp file, opens it in a **headless** Chromium page via `file://`, calls `page.pdf(path=out_pdf, landscape=True, print_background=True)`.

- [ ] **Step 1: Write the failing test (unit, mock browser)**

`tests/unit/guide/test_pdf.py`:

```python
from pathlib import Path
import pytest
from guidebot_recorder.guide.pdf import html_to_pdf


class _FakePage:
    def __init__(self): self.pdf_kwargs = None; self.url = None
    async def goto(self, url, **_): self.url = url
    async def pdf(self, **kwargs): self.pdf_kwargs = kwargs
    async def close(self): pass


class _FakeBrowser:
    def __init__(self): self.page = _FakePage()
    async def new_page(self): return self.page


async def test_pdf_called_landscape_with_background(tmp_path):
    browser = _FakeBrowser()
    out = tmp_path / "g.pdf"
    await html_to_pdf(browser, "<html><body>hi</body></html>", out)
    assert browser.page.pdf_kwargs["landscape"] is True
    assert browser.page.pdf_kwargs["print_background"] is True
    assert browser.page.url.startswith("file://")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/guide/test_pdf.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

Create `guidebot_recorder/guide/pdf.py`:

```python
"""Render composed HTML to a landscape PDF via headless Chromium page.pdf()."""

from __future__ import annotations

import tempfile
from pathlib import Path

from playwright.async_api import Browser


async def html_to_pdf(browser: Browser, html: str, out_pdf: Path) -> None:
    """Write `html` to PDF. Browser MUST be headless (page.pdf throws otherwise)."""

    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "guide.html"
        index.write_text(html, encoding="utf-8")
        page = await browser.new_page()
        try:
            await page.goto(index.absolute().as_uri(), wait_until="load")
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            await page.pdf(path=str(out_pdf), landscape=True, print_background=True)
        finally:
            await page.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/guide/test_pdf.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/guide/pdf.py tests/unit/guide/test_pdf.py
git commit -m "feat(guide): headless Chromium HTML->PDF renderer"
```

---

### Task 8: Live capture pass (`capture.py`)

**Files:**
- Create: `guidebot_recorder/guide/capture.py`
- Test: covered by the integration test in Task 9 (live browser; no isolated unit test).

**Interfaces:**
- Consumes: `Scenario`, `CompiledScenario`, a live `Page`, a `Recorder` (built by Task 9 with the same context/overlay/frame setup the renderer uses), `annotations_for` (Task 4), `classify` (Task 5), `GuidePage`/`page_text` (Task 3).
- Produces: `async capture_pages(scenario, compiled, page, recorder, shots_dir: Path, *, timeout: float, verbose: bool) -> list[GuidePage]`.

**Reference the renderer while implementing.** Read `guidebot_recorder/recorder/render.py` around `run_render` (the step loop ~1877, `skipped_branch` handling ~1862-1884/2120-2122, `reuse_is_valid` ~2384, `_resolve_url` ~1474, `apply_readiness`/`expect` ~2483-2498) and mirror the semantics. Do NOT import private render internals; re-implement the small control-flow here.

- [ ] **Step 1: Implement the capture loop**

Create `guidebot_recorder/guide/capture.py`:

```python
"""Live capture pass: replay the compiled scenario and screenshot each step."""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from guidebot_recorder.guide.annotate import annotations_for
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import classify
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.recorder.recorder import Recorder


async def _screenshot(page: Page, shots_dir: Path, index: int) -> tuple[Path, tuple[int, int]]:
    shots_dir.mkdir(parents=True, exist_ok=True)
    path = shots_dir / f"step-{index:03d}.png"
    await page.screenshot(path=str(path))
    size = page.viewport_size or {"width": 1280, "height": 720}
    return path, (size["width"], size["height"])


async def capture_pages(
    scenario,
    compiled,
    page: Page,
    recorder: Recorder,
    shots_dir: Path,
    *,
    timeout: float,
    verbose: bool = False,
) -> list[GuidePage]:
    flat = scenario.flat_steps()
    actions = compiled.actions
    pages: list[GuidePage] = []
    prev_cursor: tuple[float, float] | None = None
    skipped_branch: int | None = None

    for index, (fs, action) in enumerate(zip(flat, actions, strict=True)):
        step = fs.step
        if skipped_branch is not None:
            if fs.branch == skipped_branch:
                continue
            skipped_branch = None
        kind = classify(fs)

        if kind == "gate":
            try:
                target = action.target if isinstance(action, CachedAction) else None
                if target is None:
                    skipped_branch = fs.branch
                    continue
                await recorder.wait_for(target, "visible", timeout)
            except PlaywrightError:
                skipped_branch = fs.branch  # branch element absent -> skip whole branch
            continue

        if kind == "navigate":
            url = scenario_resolve_url(scenario, step.navigate_url())
            await recorder.navigate(url)
            shot, size = await _screenshot(page, shots_dir, index)
            pages.append(GuidePage(
                kind="navigate", screenshot=shot, text=page_text(step),
                heading=f"Otwórz adres: {url}", annotations=[], screenshot_size=size,
            ))
            prev_cursor = None
            continue

        if kind == "slide":
            s = step.slide
            pages.append(GuidePage(
                kind="slide", screenshot=None, text=s.subtitle or s.notes or "",
                heading=s.title, annotations=[],
            ))
            continue

        if kind == "text":
            pages.append(GuidePage(
                kind="text", screenshot=None, text=page_text(step), heading=None, annotations=[]
            ))
            continue

        if kind == "wait":
            if isinstance(step.wait, int | float):
                await recorder.wait_seconds(float(step.wait))
            continue

        # kind == "action": click / hover / type (dispatch on cached.action)
        if not isinstance(action, CachedAction):
            if step.optional:
                continue  # optional branch never compiled -> skip page
            raise RuntimeError(f"krok {index}: nierozwiązana akcja obowiązkowa")
        act = action.action
        try:
            res = await recorder.point(action.target, ripple=False)
        except PlaywrightError:
            if step.optional:
                continue
            raise
        if act == "type":
            text = (step.enter_text.text if step.enter_text else None) or action.input_text or ""
            await res.locator.fill(text)
            shot, size = await _screenshot(page, shots_dir, index)  # frame AFTER typing
        else:
            shot, size = await _screenshot(page, shots_dir, index)  # frame BEFORE click/hover
            if act == "hover":
                await res.locator.hover()
            else:
                await res.locator.click()
        await recorder.apply_readiness(action.expect)
        pages.append(GuidePage(
            kind="step", screenshot=shot, text=page_text(step),
            heading=None,
            annotations=annotations_for(act, prev_cursor=prev_cursor, center=res.center, box=res.box),
            screenshot_size=size,
        ))
        prev_cursor = res.center

    return pages
```

Add a tiny URL helper at the bottom of the file (or import render's `_resolve_url` logic — but keep it self-contained; read `render.py:1474-1478` for the exact base-url join and replicate):

```python
def scenario_resolve_url(scenario, url: str | None) -> str:
    """Resolve a possibly-relative navigate URL against the scenario base_url."""
    from urllib.parse import urljoin
    base = scenario.config.base_url
    if not url:
        return base
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base if base.endswith("/") else base + "/", url.lstrip("/"))
```

Note on `action.input_text` / `action.expect`: confirm these attribute names against `models/action.py` (the renderer reads `cached.input_text` at render.py:2468 and `cached.expect` for readiness). If the field is named differently, use the real name.

- [ ] **Step 2: Commit (integration test lands in Task 9)**

```bash
git add guidebot_recorder/guide/capture.py
git commit -m "feat(guide): live capture pass mirroring renderer control flow"
```

---

### Task 9: `run_guide` orchestration + CLI command + integration test

**Files:**
- Create: `guidebot_recorder/guide/guide.py`
- Modify: `guidebot_recorder/cli.py` (new `guide` command; imports)
- Create: `tests/integration/test_guide.py`

**Interfaces:**
- Consumes: everything above; `load_scenario`, `load_compiled`, `compiled_path` (see how `render.py`/`cli.py` import these), `Overlay`, `install_context`/context setup from render, `async_playwright`.
- Produces:
  - `async run_guide(path: Path, out_pdf: Path, browser: Browser, *, env=None, timeout=15.0, verbose=False) -> int` — returns page count. Loads scenario + compiled, runs `scan_for_blockers`, builds the context **the same way `run_render` does** (viewport/locale from config; chrome shell + `frame=site_frame` when `config.chrome.enabled`; overlay installed), runs `capture_pages`, then `render_html` + `html_to_pdf`.
  - CLI: `guidebot guide PATH -o OUT.pdf [--timeout] [--verbose]`.

- [ ] **Step 1: Write the failing integration test**

`tests/integration/test_guide.py` (model it on `tests/integration/test_compile_render.py` — read that file for the exact compile fixture/reasoner harness and reuse it to produce a `*.compiled.yaml` from `tests/integration/fixtures/app.html`, then run the guide):

```python
import pytest

pytestmark = pytest.mark.integration


async def test_guide_produces_pdf_with_expected_pages(tmp_path, ...):  # reuse compile harness fixtures
    # 1. Author a tiny scenario over fixtures/app.html (navigate + one click + one enter_text).
    # 2. Compile it (reuse the offline reasoner used by test_compile_render).
    # 3. Run guide:
    from playwright.async_api import async_playwright
    from guidebot_recorder.guide.guide import run_guide
    out = tmp_path / "guide.pdf"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            count = await run_guide(scenario_path, out, browser, timeout=10.0)
        finally:
            await browser.close()
    assert out.exists() and out.stat().st_size > 0
    assert count >= 2  # navigate page + at least one action page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_guide.py -v -m integration`
Expected: FAIL — `run_guide` does not exist.

- [ ] **Step 3: Implement `run_guide`**

Create `guidebot_recorder/guide/guide.py`. Read `run_render` in `render.py` (lines ~1605-1740) for the exact context/shell/overlay/`site_frame` setup and copy the minimal subset (no video recording, no TTS). Skeleton:

```python
"""Public entry point for `guidebot guide`: compiled scenario -> PDF."""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Browser

from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.layout import render_html
from guidebot_recorder.guide.pdf import html_to_pdf
from guidebot_recorder.guide.prolog import scan_for_blockers
# reuse the same loaders the renderer uses (match imports in render.py):
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled


async def run_guide(
    path: Path,
    out_pdf: Path,
    browser: Browser,
    *,
    env: dict | None = None,
    timeout: float = 15.0,
    verbose: bool = False,
) -> int:
    scenario = load_scenario(path)
    compiled = load_compiled(compiled_path(path))
    if len(compiled.actions) != len(scenario.flat_steps()):
        raise ValueError("compiled.yaml nie pasuje do scenariusza — uruchom `compile`")
    scan_for_blockers(scenario.flat_steps(), compiled.actions)

    # --- build the SAME context the renderer builds (minus video/TTS) ---
    # Read render.py run_render: context = browser.new_context(viewport=..., locale=...)
    # then, if scenario.config.chrome.enabled, mount the shell and drive the site iframe:
    #   page = await context.new_page(); overlay = Overlay(...); await overlay.install_context(context)
    #   site_frame = <the iframe frame> (chrome case) else page
    #   recorder = Recorder(page, overlay, frame=site_frame, type_delay_ms=None)
    # Keep this faithful to render.py; do not record video.
    ...

    shots_dir = out_pdf.parent / (out_pdf.stem + "_shots")
    pages = await capture_pages(
        scenario, compiled, page, recorder, shots_dir, timeout=timeout, verbose=verbose
    )
    html = render_html(pages, title=scenario.config.name)
    await html_to_pdf(browser, html, out_pdf)
    return len(pages)
```

Fill the `...` by faithfully copying the renderer's context/overlay/shell/site-frame setup (the implementer reads render.py and mirrors only the non-video parts). The `Recorder` is built with an overlay (so `point()` yields geometry) and `type_delay_ms=None` (instant fill).

- [ ] **Step 4: Wire the CLI command**

In `guidebot_recorder/cli.py`, add near the other commands (mirror `render_cmd`, cli.py:147):

```python
@app.command("guide")
def guide_cmd(
    path: Path,
    out: Path = typer.Option(..., "--out", "-o", help="Ścieżka wyjściowa .pdf"),
    timeout: float = typer.Option(15.0, "--timeout", help="Timeout akcji Playwrighta (sekundy)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Pokaż postęp"),
) -> None:
    """Zbuduj przewodnik PDF krok-po-kroku ze skompilowanego scenariusza (0×LLM)."""
    from guidebot_recorder.guide.guide import run_guide
    from guidebot_recorder.guide.prolog import GuideError

    async def _run() -> int:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)  # page.pdf() needs headless
            try:
                return await run_guide(path, out, browser, timeout=timeout, verbose=verbose)
            finally:
                await browser.close()

    try:
        count = asyncio.run(_run())
    except GuideError as exc:
        typer.echo(f"BŁĄD: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"zbudowano przewodnik: {out} ({count} stron)")
```

- [ ] **Step 5: Run the integration test + full suite**

Run: `.venv/bin/pytest tests/integration/test_guide.py -v -m integration`
Expected: PASS.
Run: `.venv/bin/pytest -q` (ensure no renderer regressions).
Expected: PASS (or only pre-existing skips for network/ffmpeg).

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/guide/guide.py guidebot_recorder/cli.py tests/integration/test_guide.py
git commit -m "feat(guide): run_guide orchestration + `guidebot guide` CLI command"
```

---

### Task 10: Docs, example, CLI reference

**Files:**
- Modify: `README.md` (mention `guide` in the pipeline/commands)
- Create: `docs/en/pdf-guide.md`, `docs/pl/pdf-guide.md`
- Modify: `docs/en/cli-reference.md`, `docs/pl/cli-reference.md`, `mkdocs.yml` (nav entries)
- Modify: `examples/login.scenario.yaml` (add one `caption:` to demonstrate the override — optional, keep the video unaffected)

**Interfaces:** none (documentation only).

- [ ] **Step 1: Write the docs**

Add a short EN + PL page describing: what `guide` does, `guidebot guide scenario.yaml -o out.pdf`, that it needs a prior `compile`, the annotation legend (strzałka ruchu / czerwone kółko = klik / czerwona ramka = wpisany tekst), landscape one-step-per-page layout, `caption:` override, and current v1 limits (single language, no popups, no numbered grouping). Add both to `mkdocs.yml` nav and cross-link from `cli-reference.md`.

- [ ] **Step 2: Verify docs build (if mkdocs available)**

Run: `.venv/bin/mkdocs build --strict 2>/dev/null || echo "mkdocs not installed — skip"`
Expected: PASS or explicit skip.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/ mkdocs.yml examples/login.scenario.yaml
git commit -m "docs(guide): document `guidebot guide` PDF output (EN+PL)"
```

---

## Self-Review notes

- Spec coverage: caption (T1), Recorder.point/B1 (T2), model+page_text/B5 (T3), annotate/B2-geometry (T4), popup+pending prolog/B3/B6 + classification (T5), layout/S-nits (T6), headless PDF/S4 (T7), skipped-branch/identity/expect/B2/B4/S1/S3/S5 (T8), CLI+run_guide+integration (T9), docs (T10). Popup fail-fast and mandatory-pending are enforced in T5's `scan_for_blockers`, called from T9's `run_guide`.
- Type consistency: `PointResult(locator, box, center)` used identically in T2/T8; `annotations_for(action, *, prev_cursor, center, box)` signature matches T4↔T8; `GuidePage` fields match T3↔T6↔T8.
- Confirmed against real code: `CachedAction(action, target, identity?, expect, state?, opens_popup, input_text?, fingerprint)`; `PendingAction(pending=True, fingerprint)`; `ActionKind = Literal["click","hover","type","waitFor"]`; `Expect = Literal["navigation","idle","none"]`; `Target` union with `RoleTarget(role, name)`; loaders `load_scenario` (`scenario.loader`), `load_compiled`/`compiled_path` (`scenario.compiled`).
- Left to the implementer (flagged inline, cheap to verify): the renderer's exact context/shell/`site_frame`/overlay setup to mirror in T8/T9 (read `render.py` ~1605-1740), and `Config` required fields for the T5 fixture.
