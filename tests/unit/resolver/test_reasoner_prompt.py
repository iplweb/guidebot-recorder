"""Co model dostaje i co wolno mu odesłać: prompt i schemat odpowiedzi.

Tu mieszkają testy `_build_prompt` (czego prompt nie może pokazywać, co musi
udokumentować, jak etykietuje kanał feedbacku) oraz `_response_schema_json`
(brak `nth`, obecność `candidateId`, brak mutacji współdzielonego schematu
`Target`). Jeden test jest asynchroniczny i idzie przez `CodexReasoner.resolve` —
sprawdza redakcję snapshotu kandydata, a jedyną drogą do zbudowanego promptu jest
podmieniony `_run_codex`.

Parsowanie odpowiedzi jest w `test_reasoner.py`, zachowanie `resolve`
w `test_reasoner_resolve.py`, a reguły `candidateId`/`nth` egzekwowane na
odpowiedzi — w `test_reasoner_targeting.py`.
"""

from __future__ import annotations

import json

import pytest

import guidebot_recorder.resolver.reasoner as reasoner_module
from guidebot_recorder.resolver.reasoner import (
    CodexReasoner,
    ReasonerError,
    _build_prompt,
    _response_schema_json,
)

from ._reasoner_helpers import _candidate, _framed


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


def _success_branches(schema: dict[str, object]) -> list[dict[str, object]]:
    branches = schema["oneOf"]
    assert isinstance(branches, list)
    return [branch for branch in branches if "action" in branch["properties"]]


def test_model_schema_drops_nth_and_offers_candidate_id():
    schema = json.loads(_response_schema_json())

    role_target = schema["$defs"]["RoleTarget"]["properties"]
    assert "nth" not in role_target
    # ``scope`` is the narrowing mechanism that replaces the index.
    assert "scope" in role_target

    success = _success_branches(schema)
    assert len(success) == 2
    for branch in success:
        assert branch["properties"]["candidateId"] == {"type": "string", "minLength": 1}
        # An answer may still omit it; the caller decides what to do then.
        assert "candidateId" not in branch["required"]

    (error_branch,) = [b for b in schema["oneOf"] if "error" in b["properties"]]
    assert "candidateId" not in error_branch["properties"]


def test_model_schema_post_processing_does_not_mutate_the_shared_target_schema():
    first = json.loads(_response_schema_json())
    second = json.loads(_response_schema_json())

    assert first == second
    assert "nth" not in second["$defs"]["RoleTarget"]["properties"]


def test_prompt_never_shows_nth_and_documents_targeting_rules():
    prompt = _build_prompt("Kliknij ×", [_candidate()])

    assert '"nth"' not in prompt
    assert "Targeting rules:" in prompt
    assert "Never return an index" in prompt
    assert '"scope"' in prompt
    assert '"candidateId"' in prompt


def test_prompt_omits_the_caller_metadata_section_without_feedback():
    prompt = _build_prompt("Kliknij ×", [_candidate()])

    assert "CALLER_METADATA_NOT_INSTRUCTIONS" not in prompt


def test_feedback_reaches_the_prompt_unlabelled_as_trusted_or_as_instruction():
    feedback = "candidate-7a02c96572535b5f matched 0 of 11 elements"

    prompt = _build_prompt("Kliknij ×", [_candidate()], feedback=feedback)

    assert "BEGIN_CALLER_METADATA_NOT_INSTRUCTIONS" in prompt
    assert feedback in prompt

    section = prompt[prompt.index("BEGIN_CALLER_METADATA_NOT_INSTRUCTIONS") :]
    assert feedback in section
    # The whole trust model rests on the labels; this channel must claim neither.
    assert "TRUSTED" not in section
    assert "AUTHOR_INSTRUCTION" not in section
    # And it must not be smuggled into a section that does claim one.
    trusted = prompt[
        prompt.index("TRUSTED_AUTHOR_INSTRUCTION_JSON") : prompt.index(
            "BEGIN_CALLER_METADATA_NOT_INSTRUCTIONS"
        )
    ]
    assert feedback not in trusted
