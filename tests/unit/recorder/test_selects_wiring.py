"""Wiring of the DOM select shim into every phase that drives pages (spec §1/§5).

Four browser contexts drive scenario steps and therefore install the widget:
``run_compile_in_browser``, the render context, ``replay_setup`` and the PDF
guide's ``run_guide``. Two others deliberately do not — ``check_logged_in`` (a
headless probe) and ``_manual_finish`` (a human's browser) — and that omission is
asserted here so a future "install it everywhere" refactor fails loudly instead
of silently shimming a live operator's dropdowns.

The render context's own installation (and its before-``chrome.js`` ordering) is
covered in ``test_render.py``, where a full render is already paid for.
"""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Browser, async_playwright
from pydantic import ValidationError

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.guide.guide import run_guide
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, SelectsConfig, TtsConfig, Viewport
from guidebot_recorder.models.scenario import (
    Scenario,
    Select,
    Step,
    StepPathError,
    select_mode,
)
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
    run_render,
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
    TargetResolutionError,
    compiled_from,
    step_instruction,
)
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import Selects, SelectsNotReadyError, install_selects

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
    # the scenario loads, naming the step and the setting that fights it. The
    # step is named by `StepPathError.path`, which the loader turns into
    # `plik:linia` + the YAML fragment — see `tests/unit/scenario/
    # test_loader_validation.py` for the banner this produces end to end.
    with pytest.raises(ValidationError) as excinfo:
        Scenario(config=_config(selects=SelectsConfig(mode="native")), steps=[shim_step])

    origin = excinfo.value.errors()[0]["ctx"]["error"]
    assert isinstance(origin, StepPathError)
    assert origin.path == (0,)
    assert "config.selects.mode: native" in str(excinfo.value)


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


async def test_guide_context_installs_the_shim(tmp_path: Path, browser, installs) -> None:
    """The PDF guide photographs the DOM the render films, so it needs the same one.

    Without this the guide's `select:` page shows a collapsed control that has
    silently changed value — the exact complaint the shim exists to answer.
    """

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(selects_block="  selects: {settleMs: 20}\n"), encoding="utf-8")
    await run_compile_in_browser(path, browser, _MockReasoner())
    installs.clear()  # the compile context's own install is not what this asserts

    await run_guide(path, tmp_path / "guide.pdf", browser, timeout=10.0)

    assert len(installs) == 1


async def test_guide_context_installs_nothing_in_native_mode(
    tmp_path: Path, browser, installs
) -> None:
    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(selects_block="  selects: {mode: native}\n"), encoding="utf-8")
    await run_compile_in_browser(path, browser, _MockReasoner())
    installs.clear()

    await run_guide(path, tmp_path / "guide.pdf", browser, timeout=10.0)

    assert installs == []


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
        #: raise the readiness barrier's failure instead of the drive failure
        self.not_ready = False
        self.calls: list[tuple[str, bool]] = []

    async def select(self, target, option: str, *, native: bool = False) -> None:
        self.calls.append((option, native))
        if self.not_ready:
            raise SelectsNotReadyError(
                "widget select nie zgłosił gotowości w ciągu 15.0 s dla ramki about:blank"
            )
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
    with pytest.raises(RenderError) as excinfo:
        await _run_render_select(_select_step(), _config(), fail=True)

    # A scenario built in code has no source map, so the banner degrades to the
    # bare step number — the located variant is the test below.
    assert "krok 8/0" in str(excinfo.value)
    assert "Województwo" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# The shim's own failures reach the author through the step diagnostics
# --------------------------------------------------------------------------- #

#: A loadable scenario whose second step is a `select:`. Line 7 opens that step.
SELECT_SCENARIO = (
    "config:\n"
    "  title: Wybór\n"
    "  viewport: {width: 640, height: 480}\n"
    "  tts: {provider: fake, voice: v, lang: pl-PL}\n"
    "steps:\n"
    f'  - navigate: "{SELECT_PAGE}"\n'
    "  - select:\n"
    '      from: "Województwo"\n'
    '      option: "Mazowieckie"\n'
)


def _located_select(tmp_path: Path):
    """``(scenario, entry, index, total, path)`` for the `select:` step of SELECT_SCENARIO.

    Loaded through ``load_scenario`` on purpose: only that attaches the source
    map, and the source map is the whole point of these tests.
    """

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(SELECT_SCENARIO, encoding="utf-8")
    scenario = load_scenario(path, env={})
    flat = scenario.flat_steps()
    return scenario, flat[1], 1, len(flat), path


async def test_render_select_drive_failure_points_at_the_line_to_edit(tmp_path: Path) -> None:
    """`SelectDriveError` must arrive *through* the diagnostics, not beside them.

    Naming the widget is not enough: the author's next move is to edit this
    step — add `mode: native`, fix the option — and the message has to say which
    line that is. A `click:` step in the same file already gets this, and the
    two must not diverge.
    """

    scenario, entry, index, total, path = _located_select(tmp_path)
    page = _FakePage()

    with pytest.raises(RenderError) as excinfo:
        await _render_step(
            page,
            _FakeRecorder(page, fail=True),
            _FakeOverlay(),
            None,
            scenario,
            entry.step,
            "select",
            index,
            None,
            0.0,
            {},
            _noop_ensure_card,
            entry=entry,
            total=total,
            resolved=_resolved_select(),
        )

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert '      from: "Województwo"' in message  # dosłowny fragment YAML
    assert "nie udało się wysterować widgetu 'Województwo'" in message


async def test_render_select_readiness_failure_points_at_the_line_to_edit(
    tmp_path: Path,
) -> None:
    """`SelectsNotReadyError` is a step failure too, and gets the same banner.

    ``Recorder.select`` raises it for a frame whose widget never settled. Both
    fixes it names — `selects.settleMs`, `selects.mode: native` — are edits to
    the very file the banner now quotes.
    """

    scenario, entry, index, total, path = _located_select(tmp_path)
    page = _FakePage()
    recorder = _FakeRecorder(page)
    recorder.not_ready = True

    with pytest.raises(RenderError) as excinfo:
        await _render_step(
            page,
            recorder,
            _FakeOverlay(),
            None,
            scenario,
            entry.step,
            "select",
            index,
            None,
            0.0,
            {},
            _noop_ensure_card,
            entry=entry,
            total=total,
            resolved=_resolved_select(),
        )

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert "nie zgłosił gotowości" in message


async def test_compile_select_drive_failure_points_at_the_line_to_edit(
    tmp_path: Path, monkeypatch
) -> None:
    """Compile's half of the same contract — the phase that fails first."""

    scenario, entry, index, total, path = _located_select(tmp_path)
    page = _FakePage()

    async def fake_resolve(root, step_in, kind, reasoner):
        return _resolved_select()

    monkeypatch.setattr(compile_module, "resolve_step_target", fake_resolve)

    with pytest.raises(RuntimeError) as excinfo:
        await compile_module._compile_step(
            page,
            _FakeRecorder(page, fail=True),
            scenario,
            "hash",
            index,
            entry.step,
            "select",
            object(),
            None,
            before_click=lambda: None,
            force=False,
            verbose=False,
            entry=entry,
            total=total,
        )

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert '      from: "Województwo"' in message
    assert "nie udało się wysterować widgetu 'Województwo'" in message


async def test_compile_resolver_verdicts_point_at_the_line_to_edit(
    tmp_path: Path, monkeypatch
) -> None:
    """An option the `<select>` does not offer is diagnosed like every other verdict.

    The rejection is produced deep in ``resolver/``, which knows nothing about
    source maps and must not; the banner is applied at the compile dispatch
    site, uniformly for every verdict — so a `select:` step and a `click:` step
    in the same file are diagnosed alike.
    """

    scenario, entry, index, total, path = _located_select(tmp_path)
    page = _FakePage()

    async def refusing_resolve(root, step_in, kind, reasoner):
        raise TargetResolutionError(
            "nie udało się zwalidować namiaru dla: 'Województwo' (ostatnie odrzucenie: "
            "The <select> has no option labelled 'Mazowieckie'; it offers: 'Śląskie'.)"
        )

    monkeypatch.setattr(compile_module, "resolve_step_target", refusing_resolve)

    with pytest.raises(RuntimeError) as excinfo:
        await compile_module._compile_step(
            page,
            _FakeRecorder(page),
            scenario,
            "hash",
            index,
            entry.step,
            "select",
            object(),
            None,
            before_click=lambda: None,
            force=False,
            verbose=False,
            entry=entry,
            total=total,
        )

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert "has no option labelled 'Mazowieckie'" in message


async def test_a_reasoner_exception_is_not_mistaken_for_a_resolver_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    """`SetupNeedsCompile` is control flow, and it is a ``RuntimeError`` too.

    Catching bare ``RuntimeError`` around the resolver to attach a banner would
    swallow its type and turn ``replay_setup``'s "run compile first" signal into
    an ordinary step failure. Only :class:`TargetResolutionError` is a verdict.
    """

    scenario, entry, index, total, _path = _located_select(tmp_path)
    page = _FakePage()

    async def signalling_resolve(root, step_in, kind, reasoner):
        raise SetupNeedsCompile("uruchom najpierw `guidebot compile`")

    monkeypatch.setattr(compile_module, "resolve_step_target", signalling_resolve)

    with pytest.raises(SetupNeedsCompile):
        await compile_module._compile_step(
            page,
            _FakeRecorder(page),
            scenario,
            "hash",
            index,
            entry.step,
            "select",
            object(),
            None,
            before_click=lambda: None,
            force=False,
            verbose=False,
            entry=entry,
            total=total,
        )


async def test_the_compile_readiness_barrier_points_at_the_line_to_edit(
    tmp_path: Path, browser, monkeypatch
) -> None:
    """A wedged widget stops the run — with the file, the line and the fragment.

    The barrier runs before the step's own work, so nothing downstream can
    supply the location for it; without this wiring it is the one shim failure
    that would reach the author as a bare sentence.
    """

    async def wedged(self, frame, timeout=None):
        raise SelectsNotReadyError("widget select nie zgłosił gotowości w ciągu 15.0 s")

    monkeypatch.setattr(Selects, "wait_ready", wedged)

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(), encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        await run_compile_in_browser(path, browser, _MockReasoner())

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert '  - teach: "kliknij Województwo"' in message
    assert "nie zgłosił gotowości" in message


class _SilentTts:
    """Narration this render never plays: the barrier fails before the first step."""

    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "0.1",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return 0.1


async def test_the_render_readiness_barrier_points_at_the_line_to_edit(
    tmp_path: Path, browser, monkeypatch
) -> None:
    """Render's barrier is the one that sits outside every per-step ``except``.

    Compile's runs inside the step's own try block, so it would at least be
    re-raised with the step's context; render's would otherwise escape the loop
    naked, and the phase that takes minutes is the worse one to lose it in.
    """

    path = tmp_path / "wybor.scenario.yaml"
    path.write_text(_scenario_yaml(), encoding="utf-8")
    await run_compile_in_browser(path, browser, _MockReasoner())

    async def wedged(self, frame, timeout=None):
        raise SelectsNotReadyError("widget select nie zgłosił gotowości w ciągu 15.0 s")

    monkeypatch.setattr(Selects, "wait_ready", wedged)

    with pytest.raises(RenderError) as excinfo:
        await run_render(path, tmp_path / "out.mp4", _SilentTts(), tmp_path / "cache", browser)

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:7" in message
    assert '  - teach: "kliknij Województwo"' in message
    assert "nie zgłosił gotowości" in message


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
    with pytest.raises(RuntimeError) as excinfo:
        await _run_compile_select(_select_step(), _config(), monkeypatch, fail=True)

    # No source map on a scenario built in code — the banner degrades to the
    # bare step number; ``..._points_at_the_line_to_edit`` covers the located one.
    assert "krok 8/0" in str(excinfo.value)
    assert "Województwo" in str(excinfo.value)


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
