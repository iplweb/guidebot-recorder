"""Wstrzyknięcie `cachedAction` in-place + zapis atomowy (Task 8, §4).

Kompilacja edytuje jeden plik: mutujemy bezpośrednio `CommentedMap`, dokładając
wyłącznie klucz `cachedAction` — komentarze, kolejność i formatowanie zostają.
Zapis jest atomowy (temp w tym samym katalogu → `os.replace`).
"""

from __future__ import annotations

import os
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from guidebot_recorder.models.action import CachedAction


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def inject_cached_action(
    doc: CommentedMap, step_index: int, action: CachedAction
) -> None:
    """Wpisz `action` do `doc["steps"][step_index]["cachedAction"]` (mutacja).

    Serializacja przez `model_dump(by_alias=True, exclude_none=True)` — pola None
    (np. `identity.testid`, `state`) są pomijane.
    """
    payload = action.model_dump(by_alias=True, exclude_none=True)
    doc["steps"][step_index]["cachedAction"] = payload


def atomic_write(path: Path | str, doc: CommentedMap) -> None:
    """Zapisz `doc` atomowo: plik tymczasowy obok → `os.replace` na `path`."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    yaml = _yaml()
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.dump(doc, fh)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
