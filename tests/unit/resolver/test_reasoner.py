from __future__ import annotations

import json

import pytest

import guidebot_recorder.resolver.reasoner as reasoner_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.reasoner import (
    CodexReasoner,
    ReasonerError,
    ReasonerResult,
    _parse_framed,
)


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


async def test_resolve_parses_framed_role_target(monkeypatch: pytest.MonkeyPatch):
    prompts: list[str] = []

    def fake_run_codex(prompt: str) -> str:
        prompts.append(prompt)
        return _framed(
            {
                "action": "click",
                "target": {
                    "strategy": "role",
                    "role": "button",
                    "name": "Zaloguj",
                    "exact": True,
                },
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve(
        "Kliknij przycisk Zaloguj", [_candidate()]
    )

    assert isinstance(result, ReasonerResult)
    assert result.action == "click"
    assert isinstance(result.target, RoleTarget)
    assert result.target == RoleTarget(role="button", name="Zaloguj", exact=True)
    assert len(prompts) == 1


async def test_resolve_returns_explicit_no_action_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "error": "no_action",
                "message": "Instrukcja nie opisuje żadnej akcji.",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Opisz ekran", [_candidate()])

    assert result == ReasonerError(
        reason="no_action", message="Instrukcja nie opisuje żadnej akcji."
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"action":"click"}',
        "<<<GUIDEBOT_JSON>>>{not-json}<<<END>>>",
        "<<<GUIDEBOT_JSON>>>[]<<<END>>>",
        (
            '<<<GUIDEBOT_JSON>>>{"error":"no_action","message":"one"}<<<END>>>'
            '<<<GUIDEBOT_JSON>>>{"error":"no_action","message":"two"}<<<END>>>'
        ),
    ],
)
def test_parse_framed_rejects_unframed_malformed_or_ambiguous_output(raw: str):
    with pytest.raises(ValueError):
        _parse_framed(raw)


async def test_resolve_retries_malformed_output_once(
    monkeypatch: pytest.MonkeyPatch,
):
    outputs = iter(
        [
            "not framed",
            _framed(
                {
                    "action": "click",
                    "target": {
                        "strategy": "role",
                        "role": "button",
                        "name": "Zaloguj",
                    },
                }
            ),
        ]
    )
    calls = 0

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return next(outputs)

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert isinstance(result, ReasonerResult)
    assert calls == 2


async def test_resolve_stops_after_two_malformed_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "not framed"

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    with pytest.raises(ValueError):
        await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert calls == 2


async def test_prompt_whitelists_candidate_snapshot_without_field_values(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "correct-horse-battery-staple"
    candidate = _candidate(role="textbox", name="Hasło", tag="input")
    # PageContext deliberately has no value field. Attaching one here guards against
    # accidentally serialising the whole runtime object instead of the redacted schema.
    candidate.value = secret  # type: ignore[attr-defined]
    prompts: list[str] = []

    def fake_run_codex(prompt: str) -> str:
        prompts.append(prompt)
        return _framed({"error": "no_handle", "message": "Brak uchwytu."})

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve("Wybierz pole hasła", [candidate])

    assert isinstance(result, ReasonerError)
    assert len(prompts) == 1
    assert "Hasło" in prompts[0]
    assert secret not in prompts[0]


async def test_missing_codex_cli_has_actionable_install_hint(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0

    def fake_missing_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise FileNotFoundError("codex")

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_missing_codex)

    with pytest.raises(RuntimeError, match=r"npm i -g @openai/codex"):
        await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert calls == 1
