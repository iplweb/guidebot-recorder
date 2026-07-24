"""Dispatch sites: effective mode and failure translation.

Both driven phases resolve a step's effective select mode and hand it to the
recorder as the ``native`` flag, and both translate a drive failure into their
phase's step error carrying the bare step index. The *located* variants of these
failures — the ones that also quote the file, line and YAML fragment — live in
``test_selects_wiring_diagnostics.py``. The mode resolution itself is in
``test_selects_wiring_mode.py``.
"""

from __future__ import annotations

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.config import Config
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.recorder.render import RenderError

# `_render_step` is a test seam, so the facade withholds it: import the module
# that defines it. See the render package docstring.
from guidebot_recorder.recorder.render._step import _render_step

from ._selects_wiring_helpers import (
    _config,
    _FakeOverlay,
    _FakePage,
    _FakeRecorder,
    _noop_ensure_card,
    _resolved_select,
    _select_scenario,
    _select_step,
)


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
    # bare step number — the located variant is
    # ``test_render_select_drive_failure_points_at_the_line_to_edit`` in
    # ``test_selects_wiring_diagnostics.py``.
    assert "krok 8/0" in str(excinfo.value)
    assert "Województwo" in str(excinfo.value)


async def _run_compile_select(
    step: Step, cfg: Config, monkeypatch, *, fail: bool = False
) -> _FakeRecorder:
    page = _FakePage()
    recorder = _FakeRecorder(page, fail=fail)

    async def fake_resolve(root, step_in, kind, reasoner):
        return _resolved_select()

    monkeypatch.setattr(compile_module.step, "resolve_step_target", fake_resolve)

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

    # No source map on a scenario built in code — the banner degrades to the bare
    # step number; the located variant is
    # ``test_compile_select_drive_failure_points_at_the_line_to_edit`` in
    # ``test_selects_wiring_diagnostics.py``.
    assert "krok 8/0" in str(excinfo.value)
    assert "Województwo" in str(excinfo.value)
