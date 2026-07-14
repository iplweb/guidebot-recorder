"""Load a scenario YAML into a validated ``Scenario`` (with ``${ENV}`` expanded).

The source file is read-only: resolved actions are written to a separate
``*.compiled.yaml`` (see ``scenario.compiled``), so no round-trip handle is kept.
``${ENV}`` substitution (§3.2) is applied only while building the model; a missing
variable raises ``KeyError``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml import YAML

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.env import substitute_scenario_values


def _to_plain(node):
    """Reduce a ruamel structure to plain Python types (for pydantic)."""
    if isinstance(node, Mapping):
        return {str(k): _to_plain(v) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_to_plain(v) for v in node]
    if isinstance(node, bool):
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if node is None:
        return None
    return str(node)


def load_scenario(path: Path | str, env: Mapping[str, str] | None = None) -> Scenario:
    """Load and validate the source scenario at ``path`` (``env`` defaults to os.environ)."""
    path = Path(path)
    if env is None:
        env = os.environ

    yaml = YAML(typ="safe")
    data = yaml.load(path.read_text(encoding="utf-8"))

    raw = _to_plain(data)
    substituted = substitute_scenario_values(raw, env)
    return Scenario.model_validate(substituted)
