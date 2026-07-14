"""CompiledScenario — the compilation artifact kept beside the source scenario.

The source ``*.scenario.yaml`` stays clean (intents only). ``compile`` writes the
resolved actions to ``*.compiled.yaml`` as a list aligned by index to ``steps``:
steps without a target carry a ``null`` entry.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction


class CompiledScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compiler_version: int = COMPILER_VERSION
    #: source file name (informational)
    source: str
    #: actions aligned 1:1 with ``Scenario.steps``; None for steps without a target
    actions: list[CachedAction | None]
