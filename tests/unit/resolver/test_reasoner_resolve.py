"""Zachowanie `CodexReasoner.resolve`: ścieżki udane, ponowienia, brak CLI.

Tu mieszka to, co dotyczy pętli `resolve` jako całości: że zwraca `ReasonerResult`
albo `ReasonerError` dla poprawnej odpowiedzi, że kontrakt `inputText` i ścisła
walidacja pól celu odrzucają odpowiedź **dwa razy** (jedno ponowienie), że pojedyncza
zniekształcona odpowiedź jest ponawiana, a druga kończy próbę, oraz że brak binarki
`codex` daje podpowiedź instalacyjną bez ponawiania. Dochodzi do tego protokół
`Reasoner` — deklaracja i dwuargumentowy dubler z ~40 istniejących testów.

Testy z licznikiem `calls == 2` sprawdzają, że odrzucenie **nastąpiło i było
ponowione** — nie, które z dziewięciu odrzuceń zadziałało: `resolve` przepakowuje je
wszystkie w jeden generyczny `ValueError`. Treść poszczególnych komunikatów przypina
`test_reasoner.py`, wołając `_result_from_payload` wprost.

Reguły `candidateId`, `nth` i przekazywania feedbacku są w `test_reasoner_targeting.py`,
a budowa promptu w `test_reasoner_prompt.py`.
"""

from __future__ import annotations

import pytest

import guidebot_recorder.resolver.reasoner as reasoner_module
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.reasoner import (
    CodexReasoner,
    Reasoner,
    ReasonerError,
    ReasonerResult,
)

from ._reasoner_helpers import _candidate, _framed


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
