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


def test_dom_path_digest_defaults_to_none():
    assert Identity(tag="button", ancestry_digest="d1").dom_path_digest is None


def test_dom_path_digest_is_not_a_matching_criterion():
    """A drift signal for `compile`, deliberately outside `matches()`.

    The path changes on any new element among the ancestors, so comparing it
    here would stop working renders on a cosmetic page change.
    """

    frozen = Identity(tag="button", ancestry_digest="d1", dom_path_digest="candidate-aaaa")

    assert frozen.matches(
        Identity(tag="button", ancestry_digest="d1", dom_path_digest="candidate-bbbb")
    )
    assert frozen.matches(Identity(tag="button", ancestry_digest="d1"))
    assert Identity(tag="button", ancestry_digest="d1").matches(frozen)


def test_dom_path_digest_is_omitted_from_a_sidecar_when_absent():
    dumped = Identity(tag="button", ancestry_digest="d1").model_dump(
        by_alias=True, exclude_none=True
    )
    assert "dom_path_digest" not in dumped
