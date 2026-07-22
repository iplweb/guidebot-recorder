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
    _response_schema_json,
    _result_from_payload,
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


_TARGET: dict[str, object] = {"strategy": "role", "role": "button", "name": "Zaloguj"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"error": "no_action"}, "Error response must contain only error and message"),
        (
            {"error": "no_action", "message": "Brak akcji.", "candidateId": "candidate-abc"},
            "Error response must contain only error and message",
        ),
        ({"error": "nie_wiem", "message": "Brak akcji."}, "Unsupported reasoner error: 'nie_wiem'"),
        ({"error": 7, "message": "Brak akcji."}, "Unsupported reasoner error: 7"),
        ({"error": "no_action", "message": 7}, "Reasoner error message must be a string"),
        (
            {"action": "teleport", "target": _TARGET},
            "Unsupported reasoner action: 'teleport'",
        ),
        ({"action": 7, "target": _TARGET}, "Unsupported reasoner action: 7"),
        (
            {"action": "type", "target": _TARGET, "temperature": 0.7},
            "Type response contains unsupported fields",
        ),
        (
            {"action": "type", "target": _TARGET, "inputText": "   "},
            "Type response inputText must be a non-empty string",
        ),
        (
            {"action": "type", "target": _TARGET, "inputText": 7},
            "Type response inputText must be a non-empty string",
        ),
        (
            {"action": "click", "target": _TARGET, "inputText": "Ala"},
            "Non-type response must contain only action, target and candidateId",
        ),
        (
            {"action": "click", "target": _TARGET, "candidateId": "   "},
            "Reasoner candidateId must be a non-empty string",
        ),
        (
            {"action": "click", "target": _TARGET, "candidateId": 7},
            "Reasoner candidateId must be a non-empty string",
        ),
    ],
)
def test_result_from_payload_rejects_each_malformed_arm_with_its_own_message(
    payload: dict[str, object],
    message: str,
):
    """Pin eight of the nine rejections ``_result_from_payload`` raises itself.

    ``_result_from_payload`` has nine ``raise ValueError`` sites of its own. Eight
    carry a message that is constant or built only from the offending value, so
    they are pinned here by exact equality. The ninth — ``Invalid Target returned
    by Codex: ...`` — ends in a rendered pydantic ``ValidationError`` and is
    pinned separately, as tightly as that allows, in
    ``test_result_from_payload_rejects_an_invalid_target_with_a_prefixed_message``.

    Not covered here: the ``nth`` guard, which lives in ``_reject_index`` (a
    separate function this one calls), not among those nine sites.

    Most of these branches were never executed by any test: only the happy paths
    were pinned (end-to-end, through ``CodexReasoner.resolve``). The error arm is
    about to be extracted into its own helper, and a dropped branch or a reworded
    message would go unnoticed today.

    Deliberately called directly rather than through ``resolve``: that path retries
    ``_MAX_ATTEMPTS`` times and re-wraps whatever fired into one generic
    ``ValueError``, so a test written that way passes no matter which branch —
    or whether the right one — raised. The ``inputText`` branch is executed that
    way by ``test_resolve_rejects_invalid_input_text_contract_twice``: line
    coverage without behavioural proof. The rows here are the proof.
    """

    with pytest.raises(ValueError) as excinfo:
        _result_from_payload(payload)

    assert str(excinfo.value) == message


def test_result_from_payload_rejects_an_invalid_target_with_a_prefixed_message():
    """Pin the ninth rejection: the one whose tail is a pydantic render.

    Exact equality is impossible on purpose, not by omission: the message is
    ``f"Invalid Target returned by Codex: {exc}"``, and ``str(exc)`` carries
    pydantic's own rendering — the union member list, the echoed input and a
    versioned ``errors.pydantic.dev/2.13/...`` link. Pinning that whole string
    would make a pydantic upgrade fail this test for no behavioural reason.

    So the constant part is pinned by prefix, and the variable part is pinned by
    what it must still identify: the discriminator value that failed and the
    pydantic error tag. Reword the prefix, drop the ``raise``, or swallow the
    ``ValidationError`` detail, and this goes red.
    """

    with pytest.raises(ValueError) as excinfo:
        _result_from_payload(
            {"action": "click", "target": {"strategy": "xpath", "value": "//div"}}
        )

    message = str(excinfo.value)
    assert message.startswith("Invalid Target returned by Codex: ")
    tail = message.removeprefix("Invalid Target returned by Codex: ")
    assert "union_tag_invalid" in tail
    assert "'xpath'" in tail


def test_result_from_payload_returns_the_error_arm_verbatim():
    """The error arm's happy path, pinned at the unit the refactor will move.

    ``test_resolve_returns_explicit_no_action_error`` covers the same payload, but
    only end-to-end through ``resolve``; this one holds once the arm lives in a
    separate helper.
    """

    result = _result_from_payload({"error": "no_handle", "message": "Brak uchwytu."})

    assert result == ReasonerError(reason="no_handle", message="Brak uchwytu.")


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
        # ``nth`` used to stand here, but it is now rejected unconditionally (the
        # model must not return an index at all), so it would pass this test for a
        # reason that has nothing to do with strict schema validation.
        ("exact", 1),
        ("exact", "true"),
    ],
)
async def test_resolve_rejects_coercible_but_schema_invalid_target_fields_twice(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
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


async def test_legacy_reasoner_stub_without_feedback_parameter_satisfies_the_protocol():
    """~40 test doubles use ``resolve(self, instruction, candidates)`` and no kwargs.

    Covers the *protocol* only: a two-argument ``resolve`` still types as a
    :class:`Reasoner` and answers when called directly. It says nothing about the
    caller — this stub would answer the same whatever ``resolve_step_target``
    did with it. The behaviour that can actually break is exercised where it
    lives, in ``test_resolution.py``
    (``test_a_legacy_double_survives_the_path_that_actually_builds_feedback``).
    """

    class LegacyStub:
        async def resolve(
            self, instruction: str, candidates: list[Candidate]
        ) -> ReasonerResult | ReasonerError:
            return ReasonerError(reason="no_action", message=instruction)

    stub: Reasoner = LegacyStub()  # type: ignore[assignment]

    assert await stub.resolve("Kliknij", [_candidate()]) == ReasonerError(
        reason="no_action", message="Kliknij"
    )


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
