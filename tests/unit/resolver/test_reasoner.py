"""Parsowanie surowej odpowiedzi Codeksa: ramka i kontrakt payloadu.

Tu mieszkają testy wołające `_parse_framed` i `_result_from_payload` **wprost**,
z gołymi słownikami — nigdy przez `CodexReasoner.resolve`. To nie jest wygoda,
tylko warunek konieczny: `resolve` ponawia próbę `_MAX_ATTEMPTS` razy i przepakowuje
każde odrzucenie w jeden generyczny `ValueError`, więc test napisany tą drogą
przechodzi niezależnie od tego, która gałąź zadziałała (patrz docstringi
poszczególnych testów). Dlatego wszystkie trzy testy charakteryzujące z fazy 0,
przypinające dziewięć komunikatów odrzucenia `_result_from_payload` przed
nadchodzącą ekstrakcją pomocnika, zostają razem w tym pliku.

Zachowanie samego `CodexReasoner.resolve` (ponowienia, ścieżki udane) jest
w `test_reasoner_resolve.py`, reguły `candidateId`/`nth`/feedbacku
w `test_reasoner_targeting.py`, a budowa promptu i schemat odpowiedzi
w `test_reasoner_prompt.py`.
"""

from __future__ import annotations

import pytest

from guidebot_recorder.resolver.reasoner import (
    ReasonerError,
    _parse_framed,
    _result_from_payload,
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
    way by ``test_resolve_rejects_invalid_input_text_contract_twice``
    (``test_reasoner_resolve.py``): line coverage without behavioural proof. The
    rows here are the proof.
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

    ``test_resolve_returns_explicit_no_action_error`` (``test_reasoner_resolve.py``)
    covers the same payload, but only end-to-end through ``resolve``; this one
    holds once the arm lives in a separate helper.
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
