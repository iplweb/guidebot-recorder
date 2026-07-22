"""Wspólni budowniczowie danych dla plików `test_reasoner*.py`.

Powstali przy podziale `test_reasoner.py` (784 linie) na cztery pliki tematyczne.
`_candidate()` trzyma minimalny, poprawny `Candidate` w jednym miejscu, więc
dodanie do modelu kolejnego pola wymaganego to jedna poprawka, nie cztery.
`_framed()` opakowuje payload w ramkę `<<<GUIDEBOT_JSON>>>…<<<END>>>` — dokładnie
tak, jak robi to prawdziwy Codex i jak musi to zrobić każdy podmieniony
`_run_codex`.

Świadomie NIE jest to `conftest.py` (decyzja D4 z
`docs/superpowers/specs/2026-07-22-code-cleanup-design.md`): pomocnik trzeba
zaimportować jawnie, żeby czytając plik testowy widzieć, skąd bierze się każda
nazwa.
"""

from __future__ import annotations

import json

from guidebot_recorder.resolver.page_context import Candidate


def _candidate(**overrides: object) -> Candidate:
    values: dict[str, object] = {
        "id": "candidate-1",
        "role": "button",
        "name": "Zaloguj",
        "tag": "button",
        "bbox": (10.0, 20.0, 100.0, 30.0),
        "visible": True,
        "enabled": True,
        "ancestry": [("main", "main")],
    }
    values.update(overrides)
    return Candidate(**values)  # type: ignore[arg-type]


def _framed(payload: object) -> str:
    return f"<<<GUIDEBOT_JSON>>>{json.dumps(payload)}<<<END>>>"
