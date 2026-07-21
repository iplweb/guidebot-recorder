"""Wiring of the DOM select shim into compile, render and setup replay (spec §1/§5).

Three browser contexts drive scenario steps and therefore install the widget:
``run_compile_in_browser``, the render context and ``replay_setup``. Two others
deliberately do not — ``check_logged_in`` (a headless probe) and
``_manual_finish`` (a human's browser) — and that omission is asserted here so a
future "install it everywhere" refactor fails loudly instead of silently
shimming a live operator's dropdowns.

The render context's own installation (and its before-``chrome.js`` ordering) is
covered in ``test_render.py``, where a full render is already paid for.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Browser, async_playwright
from pydantic import ValidationError

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, SelectsConfig, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step, select_mode
from guidebot_recorder.models.target import LabelTarget, RoleTarget
from guidebot_recorder.recorder.compile import (
    run_compile,
    run_compile_in_browser,
)
from guidebot_recorder.recorder.recorder import SelectDriveError
from guidebot_recorder.recorder.render import (
    RenderError,
    _compiled_action_is_current,
    _compiled_from,
    _render_step,
)
from guidebot_recorder.recorder.session import (
    SetupNeedsCompile,
    _manual_finish,
    check_logged_in,
    replay_setup,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    compiled_from,
    step_instruction,
)
from guidebot_recorder.selects import Selects, install_selects

# charset is explicit: without it Chromium decodes a data: URL as latin-1 and the
# Polish label no longer matches.
SELECT_PAGE = (
    "data:text/html;charset=utf-8,<label for=woj>Województwo</label>"
    "<select id=woj><option>Mazowieckie</option><option>Śląskie</option></select>"
)


def _scenario_yaml(*, selects_block: str = "") -> str:
    """A two-step scenario; ``selects_block`` is an indented ``config`` entry."""

    return (
        "config:\n"
        "  title: Wybór\n"
        "  viewport: {width: 640, height: 480}\n"
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n"
        f"{selects_block}"
        "steps:\n"
        f'  - navigate: "{SELECT_PAGE}"\n'
        '  - teach: "kliknij Województwo"\n'
    )


class _MockReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="click",
            target=LabelTarget(label="Województwo", exact=True),
        )


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        instance = await pw.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            await instance.close()


@pytest.fixture
def installs(monkeypatch) -> list[object]:
    """Record every context the widget is installed on, still installing it."""

    recorded: list[object] = []
    original = Selects.install_context

    async def spy(self, context):
        recorded.append(context)
        return await original(self, context)

    monkeypatch.setattr(Selects, "install_context", spy)
    return recorded


# --------------------------------------------------------------------------- #
# The single installation funnel
# --------------------------------------------------------------------------- #


class _FakeContext:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)


def _config(**kwargs) -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=640, height=480),
        tts=TtsConfig(provider="fake", voice="v", lang="pl-PL"),
        **kwargs,
    )


async def test_install_selects_registers_the_widget_script() -> None:
    context = _FakeContext()

    selects = await install_selects(context, _config())

    assert isinstance(selects, Selects)
    assert len(context.scripts) == 1
    assert context.scripts[0].startswith("window.__guidebot_selects_config = ")


async def test_install_selects_forwards_the_scenario_config() -> None:
    context = _FakeContext()

    await install_selects(context, _config(selects=SelectsConfig(settle_ms=42)))

    assert '"settleMs": 42' in context.scripts[0]


async def test_install_selects_installs_nothing_in_native_mode() -> None:
    """The escape hatch keeps the native control; there is no widget to inject."""

    context = _FakeContext()

    assert await install_selects(context, _config(selects=SelectsConfig(mode="native"))) is None
    assert context.scripts == []


def test_install_selects_lives_in_the_selects_package() -> None:
    """Its home is the package it installs, not the compile phase that uses it.

    ``compile`` was only ever a landlord of convenience; ``render`` and
    ``session`` import it too, so keeping it there made the shim's installation
    a detail of one phase instead of a service of the ``selects`` package.
    """

    import guidebot_recorder.selects.selects as selects_module

    assert install_selects.__module__ == selects_module.__name__
    # ``compile`` still calls it — it just no longer publishes it.
    assert "install_selects" not in compile_module.__all__


def test_run_compile_requires_an_explicit_selects_argument() -> None:
    """No default: "no shim here" must be a decision, not an omission.

    With a default, a future caller that forgets ``selects=`` silently loses the
    readiness barrier — compile then resolves targets against an unshimmed DOM
    while render drives a shimmed one, which is exactly the silent failure the
    spec's error-handling section forbids.
    """

    parameter = inspect.signature(run_compile).parameters["selects"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------- #
# Effective per-step mode (spec §5)
# --------------------------------------------------------------------------- #


def test_select_mode_inherits_the_config_mode() -> None:
    step = Step(select=Select(**{"from": "lista", "option": "Mazowieckie"}))

    assert select_mode(step, _config()) == "shim"
    assert select_mode(step, _config(selects=SelectsConfig(mode="native"))) == "native"


def test_a_step_can_opt_out_of_the_shim_but_never_opt_back_into_it() -> None:
    """The override is one-way, and the scenario says so before a browser opens.

    This used to be spelled "overrides the config mode in both directions" and
    asserted the dispatch flag `select_mode` returns — which was true and
    meaningless: under `config.selects.mode: native` nothing installs the widget,
    so a step asking for `shim` reached a page with no shim on it and failed
    mid-render, having already clicked an unrelated element on camera. The flag
    said "shim"; the outcome was a broken run.

    The reachable direction keeps working, and the unreachable one is now a load
    error, so no run can start in that state at all.
    """

    native_step = Step(
        select=Select(**{"from": "lista", "option": "Mazowieckie", "mode": "native"})
    )
    shim_step = Step(select=Select(**{"from": "lista", "option": "Mazowieckie", "mode": "shim"}))

    # Opting one control out of a global shim: supported, and the whole point.
    assert select_mode(native_step, _config()) == "native"
    assert select_mode(shim_step, _config()) == "shim"

    # Opting one control *into* a shim that was never installed: rejected while
    # the scenario loads, naming the step and the setting that fights it.
    with pytest.raises(ValidationError) as excinfo:
        Scenario(config=_config(selects=SelectsConfig(mode="native")), steps=[shim_step])

    message = str(excinfo.value)
    assert "krok 0" in message
    assert "config.selects.mode: native" in message


def test_a_steps_select_mode_is_part_of_its_own_fingerprint() -> None:
    """Deleting `mode: native` from a step must force a recompile.

    The spec claims the per-step mode "enters the fingerprint through
    `compiled_from` like any other step content". It did not: both fingerprint
    builders returned `select.from_` alone, so removing the escape hatch left
    `compile_up_to_date()` true, no browser was launched, and the drivability
    probe — one of the two mitigations the spec's error handling leans on —
    never ran.
    """

    assert compiled_from(_select_step()) == "Województwo"  # unchanged when unset
    assert compiled_from(_select_step("native")) != compiled_from(_select_step())
    assert compiled_from(_select_step("shim")) != compiled_from(_select_step("native"))
    # render and compile must agree about it, or a render would recompile forever
    assert _compiled_from(_select_step("native")) == compiled_from(_select_step("native"))


def test_the_reasoner_still_sees_only_the_step_sentence() -> None:
    """The fingerprint is not the prompt.

    `step_instruction` is what the LLM resolves against; folding a YAML
    keyword into it would put `mode: native` in front of the reasoner as if it
    were part of the author's description of the control.
    """

    assert step_instruction(_select_step("native")) == "Województwo"
    assert step_instruction(_select_step()) == "Województwo"


def test_a_frozen_action_is_stale_once_the_step_drops_its_mode() -> None:
    """The outcome the fingerprint exists for, asserted end to end."""

    frozen = CachedAction(
        action="select",
        target=RoleTarget(role="combobox", name="Województwo", exact=True),
        expect="none",
        fingerprint=Fingerprint(
            command_kind="select",
            compiled_from=compiled_from(_select_step("native")),
            expect="none",
            config_hash="h",
        ),
    )

    assert _compiled_action_is_current(_select_step("native"), frozen, "h") is True
    assert _compiled_action_is_current(_select_step(), frozen, "h") is False


# --------------------------------------------------------------------------- #
# Installation sites
# --------------------------------------------------------------------------- #


async def test_compile_context_installs_the_shim(tmp_path: Path, browser, installs) -> None:
    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(), encoding="utf-8")

    await run_compile_in_browser(path, browser, _MockReasoner())

    assert len(installs) == 1


async def test_compile_context_installs_nothing_in_native_mode(
    tmp_path: Path, browser, installs
) -> None:
    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(selects_block="  selects: {mode: native}\n"), encoding="utf-8")

    await run_compile_in_browser(path, browser, _MockReasoner())

    assert installs == []


async def test_replay_setup_installs_the_shim(tmp_path: Path, browser, installs) -> None:
    """A setup scenario with a `select:` step must behave like a target one."""

    setup = tmp_path / "logowanie.setup.yaml"
    setup.write_text(_scenario_yaml(), encoding="utf-8")

    # Never compiled → the replay fails loudly, but only *after* the context has
    # been built and wired, which is what this asserts.
    with pytest.raises(SetupNeedsCompile):
        await replay_setup(browser, setup, {}, timeout=5)

    assert len(installs) == 1


async def test_check_logged_in_does_not_install_the_shim(browser, installs) -> None:
    """A headless health probe drives no steps — nothing to make visible."""

    assert await check_logged_in(
        browser,
        {"cookies": [], "origins": []},
        goto_url="data:text/html,<p>Zalogowany</p>",
        contains_text="Zalogowany",
        locale="pl-PL",
        viewport={"width": 640, "height": 480},
        timeout=5,
    )

    assert installs == []


async def test_manual_finish_does_not_install_the_shim(browser, installs) -> None:
    """A human is using that browser and must get the real controls."""

    await _manual_finish(
        browser,
        _config(baseUrl="data:text/html,<p>Zaloguj</p>"),
        None,
        {"cookies": [], "origins": []},
        lambda _prompt: "",
    )

    assert installs == []


# --------------------------------------------------------------------------- #
# Dispatch sites: effective mode and failure translation
# --------------------------------------------------------------------------- #


class _FakePage:
    url = "https://example.test/form"

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, script, arg=None):
        return {"cursor": True, "chrome": True, "mounted": True}


class _FakeOverlay:
    pos = (0.0, 0.0)

    async def ensure(self, page) -> None:  # pragma: no cover - mounted page never repairs
        pass


class _FakeRecorder:
    """Records how ``select`` was dispatched; optionally refuses to drive."""

    def __init__(self, page: _FakePage, *, fail: bool = False) -> None:
        self.page = page
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []

    async def select(self, target, option: str, *, native: bool = False) -> None:
        self.calls.append((option, native))
        if self.fail:
            raise SelectDriveError("nie udało się wysterować widgetu 'Województwo'")

    async def apply_readiness(self, expect) -> None:
        pass


def _select_step(mode: str | None = None) -> Step:
    payload = {"from": "Województwo", "option": "Mazowieckie"}
    if mode is not None:
        payload["mode"] = mode
    return Step(select=Select(**payload))


def _select_scenario(step: Step, cfg: Config) -> Scenario:
    return Scenario(config=cfg, steps=[step])


def _resolved_select() -> ResolvedTarget:
    return ResolvedTarget(
        action="select",
        target=RoleTarget(role="combobox", name="Województwo", exact=True),
        locator=object(),
        input_text=None,
        state=None,
        identity=None,
    )


async def _noop_ensure_card(page) -> None:
    pass


async def _run_render_select(step: Step, cfg: Config, *, fail: bool = False) -> _FakeRecorder:
    page = _FakePage()
    recorder = _FakeRecorder(page, fail=fail)
    await _render_step(
        page,
        recorder,
        _FakeOverlay(),
        None,
        _select_scenario(step, cfg),
        step,
        "select",
        7,
        None,
        0.0,
        {},
        _noop_ensure_card,
        resolved=_resolved_select(),
    )
    return recorder


async def test_render_select_uses_the_config_mode_by_default() -> None:
    recorder = await _run_render_select(_select_step(), _config())

    assert recorder.calls == [("Mazowieckie", False)]


async def test_render_select_honours_the_per_step_override() -> None:
    """Only one direction reaches a render: `mode: shim` under a global `native`
    no longer loads at all (``Scenario`` rejects it), so there is nothing left
    here to dispatch."""

    recorder = await _run_render_select(_select_step("native"), _config())
    assert recorder.calls == [("Mazowieckie", True)]

    recorder = await _run_render_select(_select_step("shim"), _config())
    assert recorder.calls == [("Mazowieckie", False)]


async def test_render_select_drive_failure_becomes_a_render_error_with_the_step_index() -> None:
    with pytest.raises(RenderError, match=r"krok 7:.*Województwo"):
        await _run_render_select(_select_step(), _config(), fail=True)


async def _run_compile_select(
    step: Step, cfg: Config, monkeypatch, *, fail: bool = False
) -> _FakeRecorder:
    page = _FakePage()
    recorder = _FakeRecorder(page, fail=fail)

    async def fake_resolve(root, step_in, kind, reasoner):
        return _resolved_select()

    monkeypatch.setattr(compile_module, "resolve_step_target", fake_resolve)

    await compile_module._compile_step(
        page,
        recorder,
        _select_scenario(step, cfg),
        "hash",
        7,
        step,
        "select",
        object(),
        None,
        before_click=lambda: None,
        force=False,
        verbose=False,
    )
    return recorder


async def test_compile_select_uses_the_config_mode_by_default(monkeypatch) -> None:
    recorder = await _run_compile_select(_select_step(), _config(), monkeypatch)

    assert recorder.calls == [("Mazowieckie", False)]


async def test_compile_select_honours_the_per_step_override(monkeypatch) -> None:
    recorder = await _run_compile_select(_select_step("native"), _config(), monkeypatch)
    assert recorder.calls == [("Mazowieckie", True)]

    recorder = await _run_compile_select(_select_step("shim"), _config(), monkeypatch)
    assert recorder.calls == [("Mazowieckie", False)]


async def test_compile_select_drive_failure_names_the_step_index(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match=r"krok 7:.*Województwo"):
        await _run_compile_select(_select_step(), _config(), monkeypatch, fail=True)


# --------------------------------------------------------------------------- #
# Readiness barrier
# --------------------------------------------------------------------------- #


async def test_compile_waits_for_the_widget_before_resolving(
    tmp_path: Path, browser, monkeypatch
) -> None:
    """The resolver's page snapshot must be taken against the shimmed DOM."""

    order: list[str] = []
    original_wait = Selects.wait_ready
    original_resolve = compile_module.resolve_step_target

    async def spy_wait(self, frame, timeout=15.0):
        order.append("ready")
        return await original_wait(self, frame, timeout)

    async def spy_resolve(root, step, kind, reasoner):
        order.append("resolve")
        return await original_resolve(root, step, kind, reasoner)

    monkeypatch.setattr(Selects, "wait_ready", spy_wait)
    monkeypatch.setattr(compile_module, "resolve_step_target", spy_resolve)

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(selects_block="  selects: {settleMs: 20}\n"), encoding="utf-8")

    await run_compile_in_browser(path, browser, _MockReasoner())

    assert order[: order.index("resolve") + 1] == ["ready", "resolve"]


async def test_compile_without_the_shim_takes_no_barrier(
    tmp_path: Path, browser, monkeypatch
) -> None:
    """`mode: native` installs nothing, so nothing may be awaited either."""

    waits: list[object] = []

    async def spy_wait(self, frame, timeout=15.0):  # pragma: no cover - must not run
        waits.append(frame)

    monkeypatch.setattr(Selects, "wait_ready", spy_wait)

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(selects_block="  selects: {mode: native}\n"), encoding="utf-8")

    await run_compile_in_browser(path, browser, _MockReasoner())

    assert waits == []
