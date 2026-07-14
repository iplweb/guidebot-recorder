import pytest
from pydantic import TypeAdapter, ValidationError

from guidebot_recorder.models.target import RoleTarget, Target

TA = TypeAdapter(Target)


def test_role_target_defaults():
    t = TA.validate_python({"strategy": "role", "role": "button", "name": "Zaloguj"})
    assert isinstance(t, RoleTarget)
    assert t.exact is True
    assert t.nth is None


def test_scope_is_recursive():
    t = TA.validate_python(
        {
            "strategy": "role",
            "role": "button",
            "name": "OK",
            "scope": {"strategy": "testid", "testid": "dialog"},
        }
    )
    assert t.scope.strategy == "testid"


def test_unknown_key_forbidden():
    with pytest.raises(ValidationError):
        TA.validate_python({"strategy": "role", "role": "b", "name": "x", "bogus": 1})
