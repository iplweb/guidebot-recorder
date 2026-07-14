from guidebot_recorder.models.action import CachedAction, Fingerprint
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
