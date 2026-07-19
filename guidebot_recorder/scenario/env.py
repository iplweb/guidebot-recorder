"""Substytucja `${ENV_VAR}` (§3.2).

Rozwijana WYŁĄCZNIE w polach wartości `enterText.text` i `navigate` — nigdy w
narracji/instrukcji (`say`, `teach`, `enterText.into`, `wait.until`), by sekret
nie trafił do lektora, klucza cache audio, promptu resolvera ani `compiledFrom`.
Literalne `${` zapisujemy jako `$${`. Brak zmiennej → twardy błąd (KeyError).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

#: `$${` (escape) rozpoznawany PRZED `${VAR}`, by nie odczytać zmiennej z escapu.
_TOKEN = re.compile(r"\$\$\{|\$\{(\w+)\}")


def substitute_env(value: str, env: Mapping[str, str]) -> str:
    """Zamień `${VAR}` na `env[VAR]`; `$${` → literalne `${`; brak VAR → KeyError."""

    def _repl(m: re.Match[str]) -> str:
        if m.group(0) == "$${":
            return "${"
        name = m.group(1)
        if name not in env:
            raise KeyError(name)
        return env[name]

    return _TOKEN.sub(_repl, value)


def _substitutable_values(raw: dict) -> list[str]:
    """Collect the raw ``navigate``/``enterText.text`` strings (pre-substitution)."""

    texts: list[str] = []
    for step in raw.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        nav = step.get("navigate")
        if isinstance(nav, str):
            texts.append(nav)
        elif isinstance(nav, dict) and isinstance(nav.get("url"), str):
            texts.append(nav["url"])
        enter = step.get("enterText")
        if isinstance(enter, dict) and isinstance(enter.get("text"), str):
            texts.append(enter["text"])
    return texts


def referenced_env_names(raw: dict) -> set[str]:
    """Env var names actually referenced via ``${VAR}`` (only the substitutable fields).

    Only these were expanded into navigation URLs / typed text, so only their
    values are candidate secrets. This is what redaction must key on — scanning
    the whole environment for coincidental substrings redacts unrelated words.
    """

    names: set[str] = set()
    for text in _substitutable_values(raw):
        for match in _TOKEN.finditer(text):
            if match.group(0) != "$${" and match.group(1) is not None:
                names.add(match.group(1))
    return names


def substitute_scenario_values(raw: dict, env: Mapping[str, str]) -> dict:
    """Zwróć kopię `raw` z `${ENV}` rozwiniętym tylko w `navigate` i `enterText.text`.

    Wejście nie jest mutowane. Pola narracyjne/instrukcyjne pozostają nietknięte.
    """
    out = copy.deepcopy(raw)
    for step in out.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        nav = step.get("navigate")
        if isinstance(nav, str):
            step["navigate"] = substitute_env(nav, env)
        elif isinstance(nav, dict) and isinstance(nav.get("url"), str):
            nav["url"] = substitute_env(nav["url"], env)
        enter = step.get("enterText")
        if isinstance(enter, dict) and isinstance(enter.get("text"), str):
            enter["text"] = substitute_env(enter["text"], env)
    return out
