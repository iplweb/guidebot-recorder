from __future__ import annotations

import json

import pytest

import guidebot_recorder.resolver.reasoner as reasoner_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.reasoner import (
    CodexReasoner,
    Reasoner,
    ReasonerError,
    ReasonerResult,
    _build_prompt,
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


def test_codex_reasoner_nominally_implements_reasoner_protocol():
    assert Reasoner in CodexReasoner.__mro__


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

    result = await CodexReasoner().resolve("Kliknij przycisk Zaloguj", [_candidate()])

    assert isinstance(result, ReasonerResult)
    assert result.action == "click"
    assert result.input_text is None
    assert isinstance(result.target, RoleTarget)
    assert result.target == RoleTarget(role="button", name="Zaloguj", exact=True)
    assert len(prompts) == 1


async def test_resolve_type_requires_and_returns_input_text(monkeypatch: pytest.MonkeyPatch):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "action": "type",
                "target": {
                    "strategy": "role",
                    "role": "textbox",
                    "name": "E-mail",
                    "exact": True,
                },
                "inputText": "user@example.com",
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve(
        "Wpisz user@example.com w polu E-mail",
        [_candidate(role="textbox", name="E-mail", tag="input")],
    )

    assert result == ReasonerResult(
        action="type",
        target=RoleTarget(role="textbox", name="E-mail", exact=True),
        input_text="user@example.com",
    )


async def test_resolve_type_may_omit_text_for_explicit_enter_text(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_codex(_prompt: str) -> str:
        return _framed(
            {
                "action": "type",
                "target": {
                    "strategy": "role",
                    "role": "textbox",
                    "name": "E-mail",
                },
            }
        )

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    result = await CodexReasoner().resolve(
        "pole E-mail", [_candidate(role="textbox", name="E-mail", tag="input")]
    )

    assert isinstance(result, ReasonerResult)
    assert result.action == "type"
    assert result.input_text is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": "type",
            "target": {"strategy": "role", "role": "textbox", "name": "E-mail"},
            "inputText": "",
        },
        {
            "action": "type",
            "target": {"strategy": "role", "role": "textbox", "name": "E-mail"},
            "inputText": "   ",
        },
        {
            "action": "type",
            "target": {"strategy": "role", "role": "textbox", "name": "E-mail"},
            "inputText": 123,
        },
        {
            "action": "click",
            "target": {"strategy": "role", "role": "button", "name": "Zaloguj"},
            "inputText": "unexpected",
        },
    ],
)
async def test_resolve_rejects_invalid_input_text_contract_twice(
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


@pytest.mark.parametrize(
    "payload",
    [
        '{"action":"click","action":"hover"}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
    ],
)
def test_parse_framed_rejects_duplicate_keys_and_non_finite_numbers(payload: str):
    with pytest.raises(ValueError):
        _parse_framed(f"<<<GUIDEBOT_JSON>>>{payload}<<<END>>>")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nth", "1"),
        ("exact", "true"),
    ],
)
async def test_resolve_rejects_coercible_but_schema_invalid_target_fields_twice(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
):
    calls = 0
    target: dict[str, object] = {
        "strategy": "role",
        "role": "button",
        "name": "Zaloguj",
        field: value,
    }

    def fake_run_codex(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _framed({"action": "click", "target": target})

    monkeypatch.setattr(reasoner_module, "_run_codex", fake_run_codex)

    with pytest.raises(ValueError):
        await CodexReasoner().resolve("Kliknij Zaloguj", [_candidate()])

    assert calls == 2


def test_prompt_does_not_present_invalid_placeholder_json_as_output():
    prompt = _build_prompt("Kliknij Zaloguj", [_candidate()])

    assert '"click|hover|type|waitFor"' not in prompt
    assert '"no_action|multiple_actions|no_handle"' not in prompt
    assert "<Target>" not in prompt


def test_prompt_documents_type_payload_and_automatic_popup_switching():
    prompt = _build_prompt(
        "Wpisz user@example.com w polu E-mail",
        [_candidate(role="textbox", name="E-mail", tag="input")],
    )

    assert '"action":"type"' in prompt
    assert '"inputText":"user@example.com"' in prompt
    assert "window switching are automatic" in prompt
    assert "never model the" in prompt
    assert "as a separate action" in prompt
    assert "${ENV_VAR}" in prompt
    assert "use enterText" in prompt


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
