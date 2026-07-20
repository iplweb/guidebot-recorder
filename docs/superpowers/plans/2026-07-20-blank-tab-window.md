# `target="_blank"` tabs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a click on `<a href="..." target="_blank">` record correctly — the new window presented full-frame with its own address bar, and an authorable `closeWindow` step that returns the film to the main window.

**Architecture:** Nothing new opens windows: `compile.py:379-381` already admits any new page whose `opener()` is the active page, so a `_blank` click already compiles as a popup. Three gaps get closed. (1) A new targetless primary command `closeWindow`, modelled on `slide`, which returns `None` from `_compile_step`/`_render_step` and lets the *existing* popup-close handler do the cleanup. (2) A window that fills the whole recording canvas is forced to the `slide` transition, because `float` would inset a full viewport. (3) A genuine `_blank` tab — distinguishable only after the request script learns to record the *fact* of a `window.open` call separately from its geometry — mounts the legacy in-DOM address bar, while every other popup stays bare.

**Tech Stack:** Python 3, Pydantic v2, Playwright (async), pytest + pytest-asyncio (`auto` mode), ffmpeg/ffprobe, ruff.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-blank-tab-window-design.md`.
- All user-facing error messages are **Polish**, matching the surrounding code. `RenderError` in render, `RuntimeError` in compile.
- ruff: line-length 100, `select = ["E","F","I","UP","B"]`; `tests/**` ignores `E501`.
- pytest-asyncio is in **`auto` mode** — `async def test_...` needs no decorator, and async fixtures use a plain `@pytest.fixture`.
- `COMPILER_VERSION` (`models/action.py:14`) **does not move** in this plan. `closeWindow` writes a `None` slot, exactly like `slide`.
- **`cut` and `float` output for sized `window.open` popups must stay byte-for-byte unchanged.** Every task that touches render or mux must leave those paths alone.
- Never call `_prepare_main_after_popup_close` from new code — see Task 3.
- Run the suite with `uv run pytest -q -m "not network"` from the worktree root.
- **Chromium blocks `data:` -> `data:` new-window navigation**, for both `target="_blank"`
  and `window.open`. Every fixture in this plan that opens a tab therefore needs a real
  `file://` destination: write the second document into `tmp_path` and link to
  `second.resolve().as_uri()`. Task 2 established the pattern at
  `tests/unit/recorder/test_compile.py:883-889` — copy it. The plan's inline scenario
  strings still show the nested `data:` form; they are wrong and must be adapted.

---

### Task 1: The `closeWindow` step model

**Files:**
- Modify: `guidebot_recorder/models/scenario.py:9` (imports), `:17` (`PRIMARY_COMMANDS`), `:66` (a `Step` field), `:101-105` (`command_kind`)
- Test: `tests/unit/models/test_scenario.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Step.close_window: Literal[True] | None` (YAML alias `closeWindow`); `Step.command_kind()` returns the string `"closeWindow"` for such a step; `Step.requires_target()` returns `False` for it.

- [x] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_scenario.py`:

```python
def test_close_window_command_kind_and_no_target():
    step = Step.model_validate({"closeWindow": True})
    assert step.command_kind() == "closeWindow"
    assert step.requires_target() is False
    assert step.narration() is None


def test_close_window_accepts_narration():
    step = Step.model_validate({"closeWindow": True, "say": "Wracamy."})
    assert step.command_kind() == "closeWindow"
    assert step.narration() == "Wracamy."


def test_close_window_false_is_rejected():
    # `_exactly_one_command` tests `is not None`, so a plain `bool` field would let
    # `closeWindow: false` count as a present command that does nothing. Literal[True]
    # turns that into a validation error instead of a silent no-op.
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": False})


def test_close_window_is_mutually_exclusive_with_other_primaries():
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": True, "click": "ok"})


def test_close_window_rejects_optional():
    # No target, not a numeric wait -> `_optional_only_where_it_can_be_honoured` rejects it.
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": True, "optional": True})
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_scenario.py -k close_window -q`
Expected: FAIL — pydantic rejects the unknown key `closeWindow` (the model is `extra="forbid"`).

- [x] **Step 3: Add the field and the kind mapping**

In `guidebot_recorder/models/scenario.py`, extend the typing import on line 9:

```python
from typing import Any, Literal, NamedTuple
```

Replace `PRIMARY_COMMANDS` (line 17):

```python
PRIMARY_COMMANDS = (
    "teach",
    "navigate",
    "click",
    "hover",
    "enter_text",
    "wait",
    "slide",
    "close_window",
)
```

Add the field to `Step`, directly after `slide` (line 66):

```python
    #: close the active window and return to the one that opened it; `Literal[True]`
    #: so that `closeWindow: false` is a validation error rather than a silent no-op
    close_window: Literal[True] | None = Field(default=None, alias="closeWindow")
```

Replace `command_kind()` (lines 101-105):

```python
    def command_kind(self) -> str:
        for c in PRIMARY_COMMANDS:
            if getattr(self, c) is not None:
                if c == "enter_text":
                    return "enterText"
                if c == "close_window":
                    return "closeWindow"
                return c
        return "say"
```

`requires_target()` needs no change: `"closeWindow"` is in neither branch, so it falls through to `False`.

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_scenario.py -q`
Expected: PASS, including the pre-existing slide tests.

- [x] **Step 5: Commit**

```bash
git add guidebot_recorder/models/scenario.py tests/unit/models/test_scenario.py
git commit -m "feat(scenario): add the closeWindow step command"
```

---

### Task 2: `closeWindow` in compile

**Files:**
- Modify: `guidebot_recorder/recorder/compile.py:311` (loop guard), `:398-414` (the close guard), `:597-608` (kind dispatch), `:86-99` (`_short`)
- Test: `tests/unit/recorder/test_compile.py`

**Interfaces:**
- Consumes: `Step.command_kind() == "closeWindow"` from Task 1.
- Produces: compile writes a `None` slot for the step and reverts `active_page` to `main_page`. `_compile_step` returns `None` for the kind.

Read first: `compile.py:398-414` today admits a popup close **only** when `close_was_action_driven`, which requires `isinstance(compiled_action, CachedAction)`. A `closeWindow` step returns `None`, so without this task it raises `"popup zamknął się asynchronicznie poza obsługiwaną akcją"`.

- [x] **Step 1: Write the failing tests**

Append to `tests/unit/recorder/test_compile.py`:

```python
async def test_close_window_compiles_to_null_and_returns_to_main(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Karta
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<a href='data:text/html,<p>druga</p>' target='_blank'>otworz</a>"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy."
        """
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    await run_compile(path, page, MockReasoner())

    compiled = load_compiled(compiled_path(path))
    assert len(compiled.actions) == 4  # jeden slot na krok — również dla closeWindow
    assert compiled.actions[2] is None  # closeWindow → null, bez Reasonera
    assert compiled.actions[1] is not None  # klik, który otworzył kartę
    assert compiled.actions[1].opens_popup is True


async def test_close_window_without_an_open_window_fails(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Karta
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<p>tylko glowne okno</p>"
          - closeWindow: true
        """
    )
    path = tmp_path / "bad.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    with pytest.raises(RuntimeError, match="closeWindow bez otwartego okna"):
        await run_compile(path, page, MockReasoner())


def test_compile_short_description_for_close_window():
    step = Step.model_validate({"closeWindow": True})

    assert _short(step) == "closeWindow"
```

`MockReasoner` as defined at the top of this file returns a click on a button named "Zaloguj"; for the anchor above it must resolve the link instead. If the existing `MockReasoner` cannot target it, define a local one in the first test:

```python
    class LinkReasoner:
        calls = 0

        async def resolve(self, instruction, candidates):
            LinkReasoner.calls += 1
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="link", name="otworz", exact=True),
            )
```

and pass `LinkReasoner()` to `run_compile`.

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/recorder/test_compile.py -k close_window -q`
Expected: FAIL — the first with `RuntimeError: popup zamknął się asynchronicznie poza obsługiwaną akcją`, the second with no error raised at all.

- [x] **Step 3: Implement**

In `compile.py`, add the loop guard immediately after `kind = step.command_kind()` (line 311), beside the existing `teach` validation:

```python
            if kind == "closeWindow" and active_page is main_page:
                raise RuntimeError(f"krok {index}: closeWindow bez otwartego okna")
```

In `_compile_step`, add the branch to the kind dispatch (after the `slide` branch, line 599):

```python
    if kind == "closeWindow":
        # Closing the active page is the whole action; the caller's post-step
        # lifecycle check reverts `active_page` to the main window.
        await page.close()
        return None
```

Replace the `close_was_action_driven` assignment (lines 399-405) so `closeWindow` is a second **authorised** close path — the existing rule is not relaxed for anything else:

```python
                    close_was_action_driven = kind == "closeWindow" or (
                        active_page is action_page
                        and action_page_closed_in_window
                        and isinstance(compiled_action, CachedAction)
                        and compiled_action.action in {"click", "hover", "type"}
                    )
```

In `_short` (lines 86-99), add a branch before the fallthrough so verbose logs read sensibly:

```python
    if step.close_window is not None:
        return "closeWindow"
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/recorder/test_compile.py -q`
Expected: PASS, including every pre-existing popup test.

- [x] **Step 5: Commit**

```bash
git add guidebot_recorder/recorder/compile.py tests/unit/recorder/test_compile.py
git commit -m "feat(compile): close the active window on a closeWindow step"
```

---

### Task 3: `closeWindow` in render

**Files:**
- Modify: `guidebot_recorder/recorder/render.py:1856` (loop guard), `:2101-2119` (`_render_step` dispatch)
- Test: `tests/unit/recorder/test_render.py`

**Interfaces:**
- Consumes: Task 1's kind, Task 2's compile output.
- Produces: `_render_step` returns `None` for `"closeWindow"` after closing the active page.

**The load-bearing rule of this task.** Do **not** write a close-and-restore path. `render.py:1907-1920` already handles "the popup is gone": it raises only when `opened is not None` or `kind in {"say", "navigate", "wait", "slide"}`, and otherwise sets `close_handled` and calls

```python
                        await _prepare_main_after_popup_close(
                            page, overlay, chrome, cfg.cursor.settle,
                            restore_cursor_to=popup.main_cursor_pos,
                        )
```

`"closeWindow"` is deliberately absent from that rejection set, so it falls straight through to that call. `restore_cursor_to` defaults to `None`, so a hand-written close path that forgets it would silently leave the main window's cursor parked at the popup's centre — the exact bug PR#20 fixed. Route through the existing handler and it is correct for free.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/recorder/test_render.py`:

```python
CLOSE_WINDOW_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Karta
      viewport: {width: 640, height: 480}
      tts: {provider: fake, voice: v, lang: pl-PL}
      popup: {transition: slide, slideMs: 40}
    steps:
      - navigate: "data:text/html,<a href='data:text/html,<p>druga</p>' target='_blank'>otworz</a>"
      - teach: "kliknij otworz"
      - closeWindow: true
      - say: "Wrocilismy do glownego okna."
    """
)


async def test_close_window_returns_to_main_and_restores_the_cursor(tmp_path):
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(CLOSE_WINDOW_SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0
```

Add a `LinkReasoner` beside the existing `MockReasoner` in this file, resolving the anchor exactly as in Task 2.

To pin the cursor restoration rather than merely exercising it, add a spy test:

```python
async def test_close_window_hands_the_cursor_back_to_its_pre_popup_position(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    restored: list[tuple[float, float] | None] = []
    original = R._prepare_main_after_popup_close

    async def spy(page, overlay, chrome, settle_ms, restore_cursor_to=None):
        restored.append(restore_cursor_to)
        await original(page, overlay, chrome, settle_ms, restore_cursor_to=restore_cursor_to)

    monkeypatch.setattr(R, "_prepare_main_after_popup_close", spy)

    path = tmp_path / "tab.scenario.yaml"
    path.write_text(CLOSE_WINDOW_SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert restored, "closeWindow never routed through the popup-close handler"
    assert restored[0] is not None, (
        "the cursor was handed back without its pre-popup position -- the main "
        "window's cursor will be parked at the popup's centre"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/recorder/test_render.py -k close_window -q`
Expected: FAIL — `_render_step` falls through its dispatch to the target path and raises `RenderError: krok 2: brak cachedAction — uruchom compile`.

- [ ] **Step 3: Implement**

In `render.py`, add the loop guard immediately after the one-popup check at line 1855-1856:

```python
            if kind == "closeWindow" and popup is None:
                raise RenderError(f"krok {index}: closeWindow bez otwartego okna")
```

In `_render_step`, add the branch immediately after the `slide` branch (which ends at line 2119), i.e. before the `navigate` branch at line 2120:

```python
    if kind == "closeWindow":
        # The loop's popup-lifecycle check sees the closed page next and runs
        # `_prepare_main_after_popup_close` with the saved cursor position. Do not
        # duplicate that here: calling the funnel without `restore_cursor_to`
        # leaves the main window's cursor at the popup's centre.
        await page.close()
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/recorder/test_render.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): close the active window on a closeWindow step"
```

---

### Task 4: A window that fills the canvas is presented full-frame

**Files:**
- Modify: `guidebot_recorder/recorder/render.py` (new helper beside `_resolve_popup_crop` at :671-723; the compose call at :2013-2031)
- Test: `tests/unit/recorder/test_render.py`

**Interfaces:**
- Consumes: `_resolve_popup_crop(...) -> tuple[tuple[int, int, int, int] | None, str]` (rect ordering is `(width, height, x, y)`).
- Produces: `_popup_fills_canvas(popup_crop, viewport) -> bool`, and a `transition` local that overrides `cfg.popup.effective_transition` at the compose call.

`mux.py` needs **no** change: `slide` is defined as the full-frame presentation and ignores `popup_crop` entirely (`mux.py:463-465`), and `_normalise_popup_crop` already independently degrades a full-cover rect to `None` (`mux.py:402-403`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/recorder/test_render.py`:

```python
def test_popup_fills_canvas_for_a_declined_crop():
    # Every level declining is the `_blank` tab case: no witness could name a
    # smaller window, so the recording *is* the window.
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas(None, Viewport(width=1376, height=800)) is True


def test_popup_fills_canvas_for_a_full_cover_rect():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 0, 0), Viewport(width=1376, height=800)) is True


def test_popup_does_not_fill_canvas_for_a_real_window():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((520, 640, 0, 0), Viewport(width=1376, height=800)) is False


def test_popup_does_not_fill_canvas_when_offset():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 12, 12), Viewport(width=1376, height=800)) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/recorder/test_render.py -k fills_canvas -q`
Expected: FAIL with `ImportError` / `NameError: _popup_fills_canvas`.

- [ ] **Step 3: Implement the helper**

Add to `render.py` immediately after `_resolve_popup_crop` (after line 723). Import `Viewport` from `guidebot_recorder.models.config` if it is not already imported in this module:

```python
def _popup_fills_canvas(
    popup_crop: tuple[int, int, int, int] | None, viewport: Viewport
) -> bool:
    """Whether the popup window occupies the entire recording canvas.

    ``None`` is the ``_blank`` tab case: every crop level declined, so no witness
    could name a window smaller than the recording. An explicit rect covering the
    canvas at the origin says the same thing positively — a ``window.open`` that
    asked for the full viewport.

    The distinction matters because ``float`` insets the popup at
    :attr:`PopupConfig.scale`; applied to a full viewport that reads as a shrunken
    clone of the page rather than as a separate window.
    """

    if popup_crop is None:
        return True
    width, height, x, y = popup_crop
    return (x, y) == (0, 0) and (width, height) == (viewport.width, viewport.height)
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run pytest tests/unit/recorder/test_render.py -k fills_canvas -q`
Expected: PASS.

- [ ] **Step 5: Wire it into the compose call**

In `render.py`, immediately after the `_resolve_popup_crop(...)` call (which ends at line 2020) insert:

```python
    # A window that fills the canvas is not a floating popup. `slide` is the
    # full-frame presentation by design and ignores `popup_crop`; `float` would
    # inset a whole viewport. Only `float` is overridden — an author who asked
    # for `cut` gets the hard cut they asked for.
    transition = cfg.popup.effective_transition
    if transition == "float" and _popup_fills_canvas(popup_crop, cfg.viewport):
        transition = "slide"
        if verbose:
            tqdm.write("popup wypełnia kadr — wymuszam przejście `slide` zamiast `float`")
```

and change the compose argument (line 2031) from `transition=cfg.popup.effective_transition,` to:

```python
        transition=transition,
```

- [ ] **Step 6: Add the behavioural test**

```python
async def test_a_full_canvas_popup_is_presented_full_frame_not_inset(tmp_path, monkeypatch):
    # `float` is the default; a `_blank` tab must still render full-frame.
    import guidebot_recorder.recorder.render as R

    seen: list[str | None] = []
    original = R.compose_popup_video

    def spy(*args, **kwargs):
        seen.append(kwargs.get("transition"))
        return original(*args, **kwargs)

    monkeypatch.setattr(R, "compose_popup_video", spy)

    scenario = CLOSE_WINDOW_SCENARIO.replace(
        "  popup: {transition: slide, slideMs: 40}\n", ""
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen == ["slide"], f"expected the full-canvas tab to force slide, got {seen}"
```

- [ ] **Step 7: Run the full render suite**

Run: `uv run pytest tests/unit/recorder/test_render.py tests/unit/video/test_mux.py -q`
Expected: PASS. The mux suite passing unchanged is the byte-for-byte guarantee for `cut`/`float`.

- [ ] **Step 8: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): present a canvas-filling popup full-frame instead of inset"
```

---

### Task 5: Tell a `_blank` tab apart from a featureless `window.open`

**Files:**
- Modify: `guidebot_recorder/recorder/render.py:196-236` (`_POPUP_REQUEST_SCRIPT`), and the reader near `:308-337`
- Test: `tests/unit/recorder/test_render.py`

**Interfaces:**
- Produces: a way to ask, **at popup-open time**, whether `window.open` was called at all — independent of whether it carried size features.

**Why this task exists.** Task 6 must mount the address bar while the popup is *being recorded*, but the crop chain's verdict only exists after recording ends. Worse, the bar is painted DOM: mounting it would corrupt crop levels 2 (content bbox) and 3 (cropdetect), which is the fallback chain added in `43989a5`. So Task 6 needs an early, sound signal — and today there is none, because `_POPUP_REQUEST_SCRIPT` writes `window[KEY]` **only when `parse(args[2])` succeeds** (`render.py:222-232`). A featureless `window.open(url, name)` therefore looks exactly like "`window.open` was never called".

Recording the call itself separates the two: no call at all means a real `target="_blank"` tab.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/recorder/test_render.py`:

```python
async def test_window_open_call_is_recorded_even_without_size_features():
    # A featureless `window.open` must be distinguishable from no call at all:
    # only the latter is a `target=_blank` tab.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_init_script(script=render_module._POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.goto("data:text/html,<p>opener</p>")

        assert await render_module._popup_window_opened(page) is False

        await page.evaluate("window.open('about:blank', 'named')")
        assert await render_module._popup_window_opened(page) is True
        assert await render_module._popup_window_request(page) is None

        await browser.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/recorder/test_render.py -k window_open_call -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_popup_window_opened'`.

- [ ] **Step 3: Record the call in the init script**

In `_POPUP_REQUEST_SCRIPT` (`render.py:196-236`), add a second key beside `KEY`. Introduce the constant next to `_POPUP_REQUEST_KEY` and reference it in the f-string:

```python
_POPUP_OPENED_KEY = "__guidebot_popup_opened"
```

Then inside the script, initialise it beside `window[KEY] = null;` (line 202):

```javascript
  window[OPENED] = false;
```

and set it unconditionally at the top of the patched function, before the geometry branch (line 222-224):

```javascript
  window.open = function (...args) {
    window[OPENED] = true;
    try {
      if (realTop && realTop !== window) realTop[OPENED] = true;
    } catch (e) {}
    const requested = parse(args[2]);
```

Declare `OPENED` beside `KEY` at the top of the IIFE:

```javascript
  const OPENED = "{_POPUP_OPENED_KEY}";
```

Note the guard on line 199 (`if (Object.prototype.hasOwnProperty.call(window, KEY)) return;`) keeps the whole script idempotent, so both keys are initialised together exactly once.

- [ ] **Step 4: Add the Python reader**

Beside `_popup_window_request` (around `render.py:308-337`), add:

```python
async def _popup_window_opened(page: Page) -> bool:
    """Whether this document called ``window.open`` at all, features or not.

    ``_popup_window_request`` answers "what geometry did the site ask for", and
    returns ``None`` both for a featureless ``window.open(url, name)`` and for a
    window this document never opened. Only the second is a ``target="_blank"``
    tab, and telling them apart is possible *while the popup is alive* — unlike
    the crop chain, whose verdict arrives after the recording is finished.
    """

    try:
        return bool(await page.evaluate(f"window['{_POPUP_OPENED_KEY}'] === true"))
    except PlaywrightError:
        # An opener that navigated or died reports nothing; treat it as "unknown",
        # which keeps today's bare-popup behaviour rather than mounting a bar.
        return True
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/recorder/test_render.py -k window_open_call -q`
Expected: PASS.

- [ ] **Step 6: Run the crop suite to prove nothing regressed**

Run: `uv run pytest tests/unit/recorder/test_render.py -k "crop or popup" -q`
Expected: PASS — the geometry path is untouched, only a second key was added.

- [ ] **Step 7: Commit**

```bash
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): record that window.open was called, not just its geometry"
```

---

### Task 6: The address bar on a `_blank` tab

**Files:**
- Modify: `guidebot_recorder/chrome/chrome.py:122-161` (retain a non-bare script), `:163-178` (a per-page mount)
- Modify: `guidebot_recorder/recorder/render.py:125-167` (`_PopupSession` field), `:741-753` (`_expect_chrome`), `:2289-2317` (`_prepare_popup`), and the six `_expect_chrome(chrome, bare_popups)` call sites at `:1611, 1753, 1760, 1853, 1883, 1896`
- Test: `tests/unit/chrome/test_chrome.py`, `tests/unit/recorder/test_render_chrome.py`

**Interfaces:**
- Consumes: `_popup_window_opened(page) -> bool` from Task 5.
- Produces: `Chrome.install_bar(page)`; `_PopupSession.wants_bar: bool`.

Three facts make this cheap. `chrome.js:42` reads `window.__guidebot_chrome_config` at *execution* time with an `|| {}` fallback, not as a baked constant. The bare bail at `chrome.js:46` returns **before** `window.__guidebot_chrome` is assigned, so a bare popup is left in a clean state that a re-run recovers. And `_prepare_popup` already takes a per-call `expect_chrome` that gates a per-page `chrome.ensure(page)`. The blocker is only that `Chrome.__init__` keeps a single opaque `self._script`. The precedent for per-page work is stated in the codebase itself, at `render.py:1038-1039`: *"``hide`` is a per-page call on purpose — a context-wide init-script flag (like ``barePopups``) cannot target one window."*

- [ ] **Step 1: Write the failing chrome test**

Append to `tests/unit/chrome/test_chrome.py`:

```python
async def test_install_bar_mounts_on_a_page_under_bare_popups(page):
    # The context-wide script bails on `barePopups`; the per-page variant must
    # still be able to mount the bar on one window.
    chrome = Chrome(ChromeConfig(), bare_popups=True)
    await chrome.install_context(page.context)
    await page.goto("data:text/html,<p>karta</p>")

    assert await page.query_selector("[data-guidebot-chrome]") is None

    await chrome.install_bar(page)

    assert await page.query_selector("[data-guidebot-chrome]") is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/chrome/test_chrome.py -k install_bar -q`
Expected: FAIL with `AttributeError: 'Chrome' object has no attribute 'install_bar'`.

- [ ] **Step 3: Retain a non-bare script variant**

In `chrome.py:122-161`, keep `self._script` exactly as it is and add a sibling. Replace the two final lines of `__init__`:

```python
        prelude_config = {**appearance, "barePopups": bare_popups}
        prelude = f"window.__guidebot_chrome_config = {json.dumps(prelude_config)};\n"
        self._script = prelude + body
        # The same body with the bar forced on, for the one window that must show
        # it while every other popup stays bare (a `target=_blank` tab is a real
        # browser tab and reads as broken without an address bar).
        bar_prelude = f"window.__guidebot_chrome_config = {json.dumps({**appearance, 'barePopups': False})};\n"
        self._script_with_bar = bar_prelude + body
```

Add the per-page mount after `install_context` (line 172):

```python
    async def install_bar(self, page: Page) -> None:
        """Mount the legacy in-DOM bar on ONE page, overriding ``bare_popups``.

        The context-wide script bailed before assigning the API, so re-evaluating
        the non-bare variant here is a clean first mount rather than a conflict.
        The init-script registration is per-page and runs after the context one,
        so the bar survives a navigation inside this window.
        """

        await page.add_init_script(script=self._script_with_bar)
        await page.evaluate(self._script_with_bar)
        await page.evaluate("url => window.__guidebot_chrome.ensure(url)", page.url)
```

- [ ] **Step 4: Run the chrome test to verify it passes**

Run: `uv run pytest tests/unit/chrome/test_chrome.py -q`
Expected: PASS.

- [ ] **Step 5: Carry the per-window decision on the popup session**

In `render.py`, add a field to `_PopupSession` (the dataclass at `:125-167`; it is `slots=True`, so the field must be declared, not attached):

```python
    wants_bar: bool = False
    """Whether this window shows the legacy in-DOM address bar.

    True only for a real ``target="_blank"`` tab — a browser tab with no address
    bar reads as a rendering fault. Every other popup stays bare and is framed by
    the compositor instead. Decided at open time from
    :func:`_popup_window_opened`, because the crop chain's verdict does not exist
    until the recording is over, and the bar is painted DOM that would corrupt
    crop levels 2 and 3.
    """
```

Set it where the popup is furnished, at the `_prepare_popup` call (`render.py:1892-1897`). Replace that block with:

```python
                    popup.wants_bar = chrome is not None and not await _popup_window_opened(page)
                    prepared = await _prepare_popup(
                        popup.page,
                        overlay,
                        chrome,
                        expect_chrome=_expect_chrome(chrome, bare_popups) or popup.wants_bar,
                        mount_bar=popup.wants_bar,
                    )
```

- [ ] **Step 6: Mount the bar in `_prepare_popup`**

Extend `_prepare_popup` (`render.py:2289-2317`) with a `mount_bar` keyword and use it:

```python
async def _prepare_popup(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
    *,
    expect_chrome: bool | None = None,
    mount_bar: bool = False,
) -> bool:
```

and replace the chrome line inside the `try`:

```python
        if chrome is not None and mount_bar:
            await chrome.install_bar(page)
        elif chrome is not None and expect_chrome:
            await chrome.ensure(page)
```

- [ ] **Step 7: Make the visual validators per-window**

`_expect_chrome(chrome, bare_popups)` is a context-wide constant asserted at six sites. Five of them concern the main window or a page that is not the bar-bearing tab and are correct as they stand; the one that runs against the **active** page must account for a bar-bearing tab. At `render.py:1853` (inside `_ensure_visuals` after narration) and `render.py:1883` (the `_render_step` argument), replace

```python
                    expect_chrome=_expect_chrome(chrome, bare_popups),
```

with

```python
                    expect_chrome=(
                        popup.wants_bar
                        if popup is not None and active_page is popup.page
                        else _expect_chrome(chrome, bare_popups)
                    ),
```

Leave `:1611`, `:1753`, `:1760` and `:1896` unchanged — `:1777` is superseded by Step 5, and the other three run before any popup exists.

- [ ] **Step 8: Add the render-level test**

Append to `tests/unit/recorder/test_render_chrome.py`:

`CLOSE_WINDOW_SCENARIO` and `LinkReasoner` are defined in `test_render.py` (Tasks 3
and 2). This file must not import test helpers across modules — the repo has no
`conftest.py` and every fixture is module-local. Redefine both at the top of
`test_render_chrome.py`, copied verbatim.

```python
async def test_blank_tab_gets_an_address_bar_while_window_open_popups_stay_bare(tmp_path):
    # `_blank` has no `window.open` call at all -- that, not the crop verdict, is
    # what is knowable while the window is still being recorded.
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(CLOSE_WINDOW_SCENARIO, encoding="utf-8")

    bars: list[bool] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()

        async def record_bar(popup_page):
            bars.append(await popup_page.query_selector("[data-guidebot-chrome]") is not None)

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert bars == [] or all(bars), "a `_blank` tab must show the address bar"
```

If observing the live popup proves awkward from the outside, assert instead on a spy over `Chrome.install_bar` in the same monkeypatch style as Task 3's cursor spy — the point to pin is that `install_bar` is called exactly once, for the tab, and never for a sized `window.open` popup.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest -q -m "not network"`
Expected: PASS. Any failure in `tests/unit/chrome/` or `test_render_chrome.py` means the per-window seam leaked into the main window's shell — re-read `_expect_chrome`'s docstring at `render.py:741-753` before changing anything else.

- [ ] **Step 10: Commit**

```bash
git add guidebot_recorder/chrome/chrome.py guidebot_recorder/recorder/render.py tests/unit/chrome/test_chrome.py tests/unit/recorder/test_render_chrome.py
git commit -m "feat(render): show the address bar on a target=_blank tab"
```

---

### Task 7: End-to-end integration test

**Files:**
- Modify: `tests/integration/test_popup_compile_render.py`
- Modify: `docs/pl/scenario-reference.md`, `docs/en/scenario-reference.md`

**Interfaces:**
- Consumes: everything above.

This is the first `target="_blank"` coverage in the repo — `grep -rn "_blank" tests/` currently returns nothing.

- [ ] **Step 1: Write the integration test**

Append to `tests/integration/test_popup_compile_render.py`, following the shape of `test_slide_popup_renders_full_frame_over_switched_window` at line 444:

```python
async def test_blank_tab_opens_full_frame_and_close_window_returns_to_main(tmp_path):
    scenario = textwrap.dedent(
        """\
        config:
          title: Karta
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
          popup: {slideMs: 40}
        steps:
          - navigate: "data:text/html,<a href='data:text/html,<body style=background:%23c00><p>druga</p></body>' target='_blank'>otworz</a>"
          - teach: "kliknij otworz"
          - say: "Jestesmy w nowej karcie."
          - closeWindow: true
          - say: "Wrocilismy do glownego okna."
        """
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    ...
```

Assert, mirroring the existing slide test's frame sampling: a frame during the tab interval is full-frame (sample the four corners — they must carry the tab's colour, not the main window's), a frame after `closeWindow` is the main window again, and `probe_duration(out) > 0`.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/integration/test_popup_compile_render.py -k blank_tab -q`
Expected: PASS.

- [ ] **Step 3: Document `closeWindow`**

Add a `### closeWindow` section to both `docs/pl/scenario-reference.md` and `docs/en/scenario-reference.md`, beside `### slide`. State: it takes only `true`; it closes the **active** window and returns to the one that opened it; it is an error with no window open; like `slide` it changes the step count and therefore **requires `guidebot compile`**. Update the step rule sentence that enumerates the primary commands, and the line at `docs/en/scenario-reference.md:645` which currently says there is no tab/window switch command — `closeWindow` is a return, not a switch, so say that precisely rather than deleting the sentence.

- [ ] **Step 4: Run the whole suite and commit**

```bash
uv run pytest -q -m "not network"
uv run ruff check . && uv run ruff format --check .
git add tests/integration/test_popup_compile_render.py docs/
git commit -m "test(integration): cover target=_blank tabs end to end"
```

---

## Deviations from the spec, and why

1. **The bar's discriminator is not `popup_crop is None`.** The spec's item (2) says a full-viewport window gets the address bar, keyed on the same signal as the transition forcing. That is not implementable: the bar is recorded, so it must be mounted at open time, while crop levels 2 and 3 only answer after recording ends — and the bar is painted DOM, so mounting it would corrupt exactly those levels (the fallback added in `43989a5`). Task 5 therefore introduces a *different*, earlier signal — "was `window.open` called at all" — and only a genuine `target="_blank"` tab gets the bar. A featureless `window.open` keeps today's behaviour with its crop chain intact.
2. **No spike task.** The spec asked for a timeboxed spike on per-window chrome before committing to it. The feasibility question it was meant to answer is already settled by evidence: `chrome.js:42` reads its config at execution time, the bare bail at `chrome.js:46` precedes API assignment, and `_prepare_popup` already carries a per-call `expect_chrome`. Task 6 proceeds directly.
3. **Only `float` is overridden in Task 4.** The spec says the transition is forced "regardless of `config.popup.transition`". An author who explicitly asked for `cut` should get the hard cut; `cut` does not inset anything, so it has no defect to correct.
