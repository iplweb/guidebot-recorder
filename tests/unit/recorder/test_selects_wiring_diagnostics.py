"""The shim's own failures reach the author through the step diagnostics.

Every way a `select:` step can fail — the widget refuses to drive, the readiness
barrier never settles, the resolver rejects the option — must arrive *through*
the same banner a `click:` step gets: the file, the line, the YAML fragment. The
bare-index (source-map-less) variants of the drive failures are in
``test_selects_wiring_dispatch.py``; the barrier's ordering guarantees, in
``test_selects_wiring_readiness.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.recorder.compile import run_compile_in_browser
from guidebot_recorder.recorder.render import RenderError, run_render

# `_render_step` is a test seam, so the facade withholds it: import the module
# that defines it. See the render package docstring.
from guidebot_recorder.recorder.render._step import _render_step
from guidebot_recorder.recorder.session import SetupNeedsCompile
from guidebot_recorder.resolver.resolution import TargetResolutionError
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import Selects, SelectsNotReadyError

from ._selects_wiring_helpers import (
    SELECT_PAGE,
    _FakeOverlay,
    _FakePage,
    _FakeRecorder,
    _MockReasoner,
    _noop_ensure_card,
    _resolved_select,
    _scenario_yaml,
    browser_instance,
)


@pytest.fixture
async def browser():
    async with browser_instance() as instance:
        yield instance


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

    monkeypatch.setattr(compile_module.step, "resolve_step_target", fake_resolve)

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

    monkeypatch.setattr(compile_module.step, "resolve_step_target", refusing_resolve)

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

    monkeypatch.setattr(compile_module.step, "resolve_step_target", signalling_resolve)

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
