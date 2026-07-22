"""Jak odpowiedź wskazuje element: `candidateId`, zakaz `nth`, `scope`, feedback.

Tu mieszkają reguły, które `CodexReasoner.resolve` egzekwuje na *sposobie
wskazania celu*: `candidateId` jest opcjonalny, ale jeśli jest, musi być niepustym
stringiem (i nigdy nie towarzyszy gałęzi błędu); indeks pozycyjny `nth` jest
odrzucany na **każdym** poziomie celu — także w zagnieżdżonym `scope` — bo
narzędziem zawężania jest `scope`, nie numer; feedback wołającego dociera do
promptu przez `resolve`.

Same odrzucenia są tu widziane tak, jak widzi je wołający: generyczny `ValueError`
po dwóch próbach. Dokładne komunikaty przypina `test_reasoner.py`, wołając
`_result_from_payload` wprost. Etykietowanie kanału feedbacku w samym prompcie
sprawdza `test_reasoner_prompt.py`, a resztę zachowania `resolve` —
`test_reasoner_resolve.py`.
"""

from __future__ import annotations

import pytest

import guidebot_recorder.resolver.reasoner as reasoner_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.reasoner import (
    CodexReasoner,
    ReasonerResult,
)

from ._reasoner_helpers import _candidate, _framed


async def test_resolve_accepts_and_returns_candidate_id(monkeypatch: pytest.MonkeyPatch):
    calls = 0

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _framed(
            {
                "action": "click",
                "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
                "candidateId": "candidate-7a02c96572535b5f",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert isinstance(result, ReasonerResult)
    assert result.candidate_id == "candidate-7a02c96572535b5f"
    # No retry: a well-formed answer must be accepted on the first attempt.
    assert calls == 1


async def test_resolve_accepts_candidate_id_alongside_input_text(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "action": "type",
                "target": {"strategy": "role", "role": "textbox", "name": "E-mail"},
                "inputText": "user@example.com",
                "candidateId": "candidate-abc123",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Wpisz e-mail", [_candidate(role="textbox")])

    assert result == ReasonerResult(
        action="type",
        target=RoleTarget(role="textbox", name="E-mail"),
        input_text="user@example.com",
        candidate_id="candidate-abc123",
    )


async def test_resolve_defaults_candidate_id_to_none(monkeypatch: pytest.MonkeyPatch):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "action": "click",
                "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert isinstance(result, ReasonerResult)
    assert result.candidate_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": "click",
            "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
            "candidateId": "",
        },
        {
            "action": "click",
            "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
            "candidateId": "   ",
        },
        {
            "action": "click",
            "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
            "candidateId": 7,
        },
        # The error branch stays exactly as narrow as it was.
        {"error": "no_action", "message": "Brak akcji.", "candidateId": "candidate-abc"},
    ],
)
async def test_resolve_rejects_invalid_candidate_id_twice(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
):
    calls = 0

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _framed(payload)

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    with pytest.raises(ValueError):
        await CodexReasoner().resolve("Wykonaj akcję", [_candidate()])

    assert calls == 2


@pytest.mark.parametrize(
    "target",
    [
        {"strategy": "role", "role": "button", "name": "×", "nth": 2},
        {
            "strategy": "role",
            "role": "button",
            "name": "×",
            "scope": {"strategy": "role", "role": "group", "name": "Kryteria", "nth": 1},
        },
        {
            "strategy": "role",
            "role": "button",
            "name": "×",
            "scope": {
                "strategy": "text",
                "text": "Charakter formalny",
                "scope": {"strategy": "role", "role": "group", "name": "Kryteria", "nth": 0},
            },
        },
    ],
)
async def test_resolve_rejects_nth_at_any_target_level_twice(
    monkeypatch: pytest.MonkeyPatch,
    target: dict[str, object],
):
    calls = 0

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _framed(
            {"action": "click", "target": target, "candidateId": "candidate-abc123"}
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    with pytest.raises(ValueError, match="nth"):
        await CodexReasoner().resolve("Kliknij ×", [_candidate()])

    assert calls == 2


async def test_resolve_scope_without_nth_is_accepted(monkeypatch: pytest.MonkeyPatch):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "action": "click",
                "target": {
                    "strategy": "role",
                    "role": "button",
                    "name": "×",
                    "scope": {"strategy": "text", "text": "Charakter formalny"},
                },
                "candidateId": "candidate-abc123",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Kliknij ×", [_candidate()])

    assert isinstance(result, ReasonerResult)
    assert isinstance(result.target, RoleTarget)
    assert result.target.scope is not None


async def test_resolve_forwards_feedback_to_the_prompt(monkeypatch: pytest.MonkeyPatch):
    prompts: list[str] = []

    def fake_run_codex(prompt: str) -> str:
        prompts.append(prompt)
        return _framed(
            {
                "action": "click",
                "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
                "candidateId": "candidate-abc123",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    await CodexReasoner().resolve(
        "Kliknij Zaloguj", [_candidate()], feedback="candidate-abc123 matched 2 of 5"
    )

    assert "candidate-abc123 matched 2 of 5" in prompts[0]
