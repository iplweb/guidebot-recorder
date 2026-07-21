"""Unit tests for `run_guide`'s compiled-sidecar startup checks (no browser).

Both checks must run before any browser/context work, so a bogus/None
``browser`` is enough to prove they raise ``GuideError`` (not a raw
``FileNotFoundError``/``ValueError`` traceback) before the browser is ever
touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guidebot_recorder.guide.guide import run_guide
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled

SCENARIO_YAML = """\
config:
  title: t
  viewport: {width: 800, height: 600}
  tts: {provider: fake, voice: v, lang: pl-PL}
steps:
  - say: "tylko narracja"
"""


def _write_scenario(tmp_path: Path) -> Path:
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO_YAML, encoding="utf-8")
    return path


async def test_missing_compiled_sidecar_raises_guide_error(tmp_path):
    path = _write_scenario(tmp_path)
    # deliberately no *.compiled.yaml written

    with pytest.raises(GuideError, match="compile"):
        await run_guide(path, tmp_path / "out.pdf", None)


async def test_compiled_sidecar_from_another_scenario_raises_guide_error(tmp_path):
    path = _write_scenario(tmp_path)
    compiled = CompiledScenario(source="other.scenario.yaml", actions=[None])
    write_compiled(compiled_path(path), compiled)

    with pytest.raises(GuideError, match="innego scenariusza"):
        await run_guide(path, tmp_path / "out.pdf", None)


async def test_compiled_sidecar_length_mismatch_raises_guide_error_not_value_error(tmp_path):
    path = _write_scenario(tmp_path)
    action = CachedAction(
        action="click",
        target=RoleTarget(role="button", name="x"),
        expect="none",
        fingerprint=Fingerprint(
            command_kind="click", compiled_from="x", expect="none", config_hash="c"
        ),
    )
    # source matches, but two actions for a one-step scenario -> length mismatch
    compiled = CompiledScenario(source=path.name, actions=[None, action])
    write_compiled(compiled_path(path), compiled)

    with pytest.raises(GuideError, match="liczbą krok"):
        await run_guide(path, tmp_path / "out.pdf", None)
