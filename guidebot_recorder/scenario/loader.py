"""Load a scenario YAML into a validated ``Scenario`` (with ``${ENV}`` expanded).

The source file is read-only: resolved actions are written to a separate
``*.compiled.yaml`` (see ``scenario.compiled``), so no round-trip handle is kept.
``${ENV}`` substitution (§3.2) is applied only while building the model; a missing
variable raises ``KeyError``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from ruamel.yaml import YAML

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.env import referenced_env_names, substitute_scenario_values


class CompiledSidecarError(ValueError):
    """Raised when a ``*.compiled.yaml`` sidecar is passed where a source scenario is expected.

    The generated sidecar is an input only to ``render`` (and even there the
    ``.scenario.yaml`` is passed, with the sidecar found beside it) — never to
    ``compile``/``validate``. Its top-level shape is ``compiler_version``/``source``/
    ``actions``, so validating it as a :class:`Scenario` would otherwise spew a
    confusing pydantic error about missing ``config``/``steps``.
    """


def _looks_like_compiled_sidecar(raw: object, path: Path) -> bool:
    """Heuristic: a compiled sidecar by filename, or by its distinctive top-level shape."""

    if path.name.endswith(".compiled.yaml"):
        return True
    return (
        isinstance(raw, Mapping)
        and "config" not in raw
        and "steps" not in raw
        and ("compiler_version" in raw or "actions" in raw)
    )


def _sidecar_message(path: Path) -> str:
    source = re.sub(r"\.compiled\.yaml$", ".scenario.yaml", path.name)
    hint = f"`{source}`" if source != path.name else "plik `*.scenario.yaml`"
    return (
        f"`{path.name}` to skompilowany sidecar (*.compiled.yaml), nie scenariusz źródłowy. "
        f"Podaj plik scenariusza, np. {hint}. Sidecar jest wynikiem `compile` i wejściem "
        f"tylko do `render` (i tam też podaje się `.scenario.yaml`)."
    )


def guard_source_scenario(path: Path | str) -> None:
    """Fail fast with :class:`CompiledSidecarError` if ``path`` is a compiled sidecar.

    A cheap check (raw parse only, no env substitution or full validation) so
    callers can reject the mistake with a friendly message before the heavier
    pipeline — or a missing-``${ENV}`` error — has a chance to obscure it.
    """

    path = Path(path)
    if _looks_like_compiled_sidecar(_read_raw(path), path):
        raise CompiledSidecarError(_sidecar_message(path))


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


@lru_cache(maxsize=32)
def _parse_source(source: str) -> dict:
    """Parse unchanged source text once; callers never mutate the cached value."""

    yaml = YAML(typ="safe")
    data = yaml.load(source)
    return _to_plain(data)


def _read_raw(path: Path) -> dict:
    """Read the scenario YAML into plain Python types (``${ENV}`` still intact).

    The file is read on every call, so edits are observed immediately. Only the
    deterministic YAML parse is shared when the exact source text is unchanged;
    environment substitution still receives a deep copy in
    :func:`substitute_scenario_values`.
    """

    return _parse_source(path.read_text(encoding="utf-8"))


def load_scenario(path: Path | str, env: Mapping[str, str] | None = None) -> Scenario:
    """Load and validate the source scenario at ``path`` (``env`` defaults to os.environ)."""
    if env is None:
        env = os.environ
    raw = _read_raw(Path(path))
    if _looks_like_compiled_sidecar(raw, Path(path)):
        raise CompiledSidecarError(_sidecar_message(Path(path)))
    substituted = substitute_scenario_values(raw, env)
    return Scenario.model_validate(substituted)


def scenario_env_references(
    path: Path | str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return ``{name: env[name]}` for env vars the scenario references via ``${VAR}``.

    Only these values were expanded into navigation URLs / typed text, so only
    they are candidate secrets for redaction (see ``referenced_env_names``).
    """
    if env is None:
        env = os.environ
    names = referenced_env_names(_read_raw(Path(path)))
    return {name: env[name] for name in names if name in env}
