"""CompiledScenario — the compilation artifact kept beside the source scenario.

The source ``*.scenario.yaml`` stays clean (intents only). ``compile`` writes the
resolved actions to ``*.compiled.yaml`` as a list aligned by index to ``steps``:
steps without a target carry a ``null`` entry.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction, PendingAction


def _action_tag(value: Any) -> str:
    """Discriminate a compiled entry on the ``pending`` key."""

    if isinstance(value, PendingAction):
        return "pending"
    if isinstance(value, dict):
        return "pending" if value.get("pending") else "cached"
    return "cached"


#: a resolved action, or a placeholder for one that could not be resolved yet
CompiledAction = Annotated[
    Annotated[CachedAction, Tag("cached")] | Annotated[PendingAction, Tag("pending")],
    Discriminator(_action_tag),
]


class CompiledScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compiler_version: int = COMPILER_VERSION
    #: source file name (informational)
    source: str
    #: entries aligned 1:1 with ``Scenario.flat_steps()``; None for steps without a target
    actions: list[CompiledAction | None]
