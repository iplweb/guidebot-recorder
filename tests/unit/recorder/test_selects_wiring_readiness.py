"""The readiness barrier: compile waits for the widget, and only when it exists.

Before compile resolves a target it must await the shim's readiness, so the
resolver's page snapshot is taken against the shimmed DOM — but under
``mode: native`` nothing installs, so nothing may be awaited. The banner the
barrier produces when the widget wedges is asserted in
``test_selects_wiring_diagnostics.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.recorder.compile import run_compile_in_browser
from guidebot_recorder.selects import Selects

from ._selects_wiring_helpers import _MockReasoner, _scenario_yaml, browser_instance


@pytest.fixture
async def browser():
    async with browser_instance() as instance:
        yield instance


async def test_compile_waits_for_the_widget_before_resolving(
    tmp_path: Path, browser, monkeypatch
) -> None:
    """The resolver's page snapshot must be taken against the shimmed DOM."""

    order: list[str] = []
    original_wait = Selects.wait_ready
    original_resolve = compile_module.step.resolve_step_target

    async def spy_wait(self, frame, timeout=15.0):
        order.append("ready")
        return await original_wait(self, frame, timeout)

    async def spy_resolve(root, step, kind, reasoner):
        order.append("resolve")
        return await original_resolve(root, step, kind, reasoner)

    monkeypatch.setattr(Selects, "wait_ready", spy_wait)
    monkeypatch.setattr(compile_module.step, "resolve_step_target", spy_resolve)

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
