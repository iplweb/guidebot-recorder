"""Where the DOM select shim is (and is not) installed (spec §1/§5).

Four browser contexts drive scenario steps and therefore install the widget:
``run_compile_in_browser``, the render context, ``replay_setup`` and the PDF
guide's ``run_guide``. Two others deliberately do not — ``check_logged_in`` (a
headless probe) and ``_manual_finish`` (a human's browser) — and that omission is
asserted here so a future "install it everywhere" refactor fails loudly instead
of silently shimming a live operator's dropdowns.

The render context's own installation (and its before-``chrome.js`` ordering) is
covered in ``test_render.py``, where a full render is already paid for.

The per-step *effective mode* these contexts honour is covered in
``test_selects_wiring_mode.py``; how a driven step dispatches through it, in
``test_selects_wiring_dispatch.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.guide.guide import run_guide
from guidebot_recorder.models.config import SelectsConfig
from guidebot_recorder.recorder.compile import run_compile, run_compile_in_browser
from guidebot_recorder.recorder.session import (
    SetupNeedsCompile,
    _manual_finish,
    check_logged_in,
    replay_setup,
)
from guidebot_recorder.selects import Selects, install_selects

from ._selects_wiring_helpers import (
    _config,
    _MockReasoner,
    _scenario_yaml,
    browser_instance,
    record_installs,
)


@pytest.fixture
async def browser():
    async with browser_instance() as instance:
        yield instance


@pytest.fixture
def installs(monkeypatch) -> list[object]:
    return record_installs(monkeypatch)


# --------------------------------------------------------------------------- #
# The single installation funnel
# --------------------------------------------------------------------------- #


class _FakeContext:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)


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
