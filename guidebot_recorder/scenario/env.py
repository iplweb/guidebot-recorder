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
