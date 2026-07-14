import pytest
from pydantic import ValidationError

from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget


def test_click_action():
    ca = CachedAction(
        action="click",
        target=RoleTarget(role="button", name="Zaloguj"),
        identity=Identity(tag="button", ancestry_digest="d"),
        expect="navigation",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="...",
            expect="navigation",
            compiler_version=1,
            config_hash="c",
        ),
    )
    assert ca.action == "click"
    assert ca.identity.tag == "button"


def test_waitfor_hidden_without_identity():
    ca = CachedAction(
        action="waitFor",
        state="hidden",
        target=RoleTarget(role="dialog", name="X"),
        identity=None,
        expect="none",
        fingerprint=Fingerprint(
            command_kind="wait",
            compiled_from="...",
            expect="none",
            compiler_version=1,
            config_hash="c",
            state="hidden",
        ),
    )
    assert ca.state == "hidden"
    assert ca.identity is None


def test_teach_can_freeze_literal_text_for_type():
    ca = CachedAction(
        action="type",
        target=RoleTarget(role="textbox", name="E-mail"),
        identity=Identity(tag="input", ancestry_digest="d"),
        expect="none",
        input_text="koparka@poczta.wp.pl",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="wpisz koparka@poczta.wp.pl w pole e-mail",
            expect="none",
            config_hash="c",
        ),
    )

    assert ca.input_text == "koparka@poczta.wp.pl"
    assert ca.fingerprint.compiler_version == COMPILER_VERSION


@pytest.mark.parametrize(
    ("compiled_from", "input_text"),
    [
        ("wpisz wartość z instrukcji", "wartość wymyślona"),
        ("wpisz hasło hunter2", "hunter2"),
        ("enter API key sk-live-demo", "sk-live-demo"),
    ],
)
def test_teach_type_rejects_invented_or_sensitive_values(compiled_from, input_text):
    with pytest.raises(ValidationError):
        CachedAction(
            action="type",
            target=RoleTarget(role="textbox", name="Pole"),
            identity=Identity(tag="input", ancestry_digest="d"),
            expect="none",
            input_text=input_text,
            fingerprint=Fingerprint(
                command_kind="teach",
                compiled_from=compiled_from,
                expect="none",
                config_hash="c",
            ),
        )


@pytest.mark.parametrize(
    ("action", "command_kind", "opens_popup", "input_text"),
    [
        ("type", "teach", False, None),
        ("click", "teach", False, "tekst"),
        ("type", "enterText", False, "tekst"),
        ("type", "teach", True, "tekst"),
        ("type", "teach", False, "${PASSWORD}"),
    ],
)
def test_behavioral_metadata_rejects_invalid_combinations(
    action, command_kind, opens_popup, input_text
):
    with pytest.raises(ValidationError):
        CachedAction(
            action=action,
            target=RoleTarget(role="textbox", name="E-mail"),
            identity=Identity(tag="input", ancestry_digest="d"),
            expect="none",
            opens_popup=opens_popup,
            input_text=input_text,
            fingerprint=Fingerprint(
                command_kind=command_kind,
                compiled_from="instrukcja",
                expect="none",
                config_hash="c",
            ),
        )
