from guidebot_recorder.models.identity import Identity


def test_matches_equal():
    a = Identity(tag="button", ancestry_digest="d1")
    assert a.matches(Identity(tag="button", ancestry_digest="d1"))


def test_mismatch_on_field():
    a = Identity(tag="button", testid="x", ancestry_digest="d1")
    assert not a.matches(Identity(tag="button", testid="y", ancestry_digest="d1"))


def test_version_mismatch():
    a = Identity(tag="a", ancestry_digest="d", identity_version=1)
    assert not a.matches(Identity(tag="a", ancestry_digest="d", identity_version=2))
