"""Wczytanie YAML → (Scenario, round-trip doc) (Task 7, §4).

`load_scenario` zwraca `LoadedScenario` z:
- `scenario`: zwalidowany model pydantic (z rozwiniętymi `${ENV}`),
- `doc`: surowy `CommentedMap` (round-trip handle do zapisu in-place, §4) —
  BEZ substytucji `${ENV}`, żeby sekrety nie trafiły do repo przy zapisie.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.env import substitute_scenario_values


@dataclass
class LoadedScenario:
    """Wynik wczytania: zwalidowany model + round-trip handle do zapisu."""

    scenario: Scenario
    doc: CommentedMap


def _to_plain(node):
    """Zrzuć strukturę ruamel do czystych typów Pythona (dla pydantic)."""
    if isinstance(node, Mapping):
        return {str(k): _to_plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
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


def load_scenario(
    path: Path | str, env: Mapping[str, str] | None = None
) -> LoadedScenario:
    """Wczytaj scenariusz YAML z `path`.

    `env=None` → używa `os.environ`. Substytucja `${ENV}` (§3.2) stosowana tylko
    przy budowie `Scenario`; brak zmiennej → KeyError. `doc` pozostaje surowy.
    """
    path = Path(path)
    if env is None:
        env = os.environ

    yaml = YAML(typ="rt")
    doc = yaml.load(path.read_text(encoding="utf-8"))

    raw = _to_plain(doc)
    substituted = substitute_scenario_values(raw, env)
    scenario = Scenario.model_validate(substituted)

    return LoadedScenario(scenario=scenario, doc=doc)
