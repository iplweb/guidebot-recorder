"""`reuse_is_valid` / `reuse_failure` — czy zamrożony cel wolno odtworzyć.

Oba wejścia trzymamy razem, bo `reuse_is_valid` to cienka otoczka na
`reuse_failure` i każda zmiana powodu odmowy musi być sprawdzona z obu stron:
raz jako konkretny string, raz jako `bool`.

`_cached()` zostaje lokalny dla tego pliku. Ma tu ~12 konsumentów i ani jednego
poza tym plikiem — wyniesienie go do modułu współdzielonego dołożyłoby import,
nie odjęło duplikacji.

Walidacja kompilacyjna, przez którą te ścieżki przechodzą, jest sprawdzana
osobno w `test_validate_compile_time.py`.
"""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
)
from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,
)
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.validate import (
    build_locator,
    reuse_failure,
    reuse_is_valid,
)

from ._validate_page import playwright_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with playwright_page() as browser_page:
        yield browser_page


def _cached(
    target,
    identity: Identity | None,
    action: str = "click",
    state: str | None = None,
) -> CachedAction:
    return CachedAction(
        action=action,
        target=target,
        identity=identity,
        expect="none",
        state=state,
        fingerprint=Fingerprint(
            command_kind="wait" if action == "waitFor" else "teach",
            compiled_from="kliknij Zaloguj",
            expect="none",
            config_hash="config",
            state=state,
        ),
    )


async def test_reuse_rejects_teach_type_on_password_field(page):
    await page.set_content('<label for="value">Access</label><input id="value" type="password">')
    target = LabelTarget(label="Access")
    identity = await capture_identity(await build_locator(page, target))
    cached = CachedAction(
        action="type",
        target=target,
        identity=identity,
        expect="none",
        input_text="hunter2",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="enter hunter2 into the field",
            expect="none",
            config_hash="config",
        ),
    )

    assert await reuse_is_valid(page, cached) is False


async def test_reuse_is_invalid_when_captured_identity_differs(page):
    await page.set_content("<main><button>Zaloguj</button></main>")
    cached = _cached(
        RoleTarget(role="button", name="Zaloguj"),
        Identity(tag="a", ancestry_digest="not-the-current-digest"),
    )

    assert await reuse_is_valid(page, cached) is False


async def test_reuse_is_valid_when_structural_checks_and_identity_match(page):
    await page.set_content("<main><button>Zaloguj</button></main>")
    target = RoleTarget(role="button", name="Zaloguj")
    identity = await capture_identity(await build_locator(page, target))

    assert await reuse_is_valid(page, _cached(target, identity)) is True


async def test_wait_for_hidden_cache_without_identity_can_be_reused(page):
    await page.set_content('<div data-testid="spinner">Ładowanie</div>')
    cached = _cached(
        ByTestidTarget(testid="spinner"),
        None,
        action="waitFor",
        state="hidden",
    )

    assert await reuse_is_valid(page, cached) is True


async def test_wait_for_cache_without_state_is_invalid(page):
    await page.set_content('<div data-testid="spinner">Ładowanie</div>')
    target = ByTestidTarget(testid="spinner")
    identity = await capture_identity(await build_locator(page, target))

    assert await reuse_is_valid(page, _cached(target, identity, action="waitFor")) is False


async def test_reuse_failure_returns_none_when_structural_checks_and_identity_match(page):
    await page.set_content("<main><button>Zaloguj</button></main>")
    target = RoleTarget(role="button", name="Zaloguj")
    identity = await capture_identity(await build_locator(page, target))

    assert await reuse_failure(page, _cached(target, identity)) is None


async def test_reuse_failure_returns_identity_mismatch_when_captured_identity_differs(page):
    await page.set_content("<main><button>Zaloguj</button></main>")
    cached = _cached(
        RoleTarget(role="button", name="Zaloguj"),
        Identity(tag="a", ancestry_digest="not-the-current-digest"),
    )

    assert await reuse_failure(page, cached) == "identity_mismatch"


async def test_reuse_failure_returns_not_found_when_target_missing_from_dom(page):
    await page.set_content("<main></main>")
    target = RoleTarget(role="button", name="Zaloguj")
    identity = Identity(tag="button", ancestry_digest="whatever")

    assert await reuse_failure(page, _cached(target, identity)) == "not_found"


async def test_reuse_failure_forwards_the_wanted_option_to_validation(page):
    """A frozen `select` whose option vanished must fail here, cheaply.

    `validate_compile_time` grew the `option` parameter so a plausible-but-wrong
    dropdown is rejected by reading `select.options` instead of by a 15s
    `select_option(label=…)` timeout. `reuse_failure` never passed it on, so the
    only caller that *has* the label — `guide`, which reads it off the scenario
    step — could not use the check at all: `option_missing` was unreachable.
    """

    await page.set_content(
        '<select aria-label="Rodzaj raportu">'
        "<option>Raport jednostki</option><option>Raport autora</option>"
        "</select>"
    )
    target = RoleTarget(role="combobox", name="Rodzaj raportu")
    identity = await capture_identity(await build_locator(page, target))
    cached = _cached(target, identity, action="select")

    assert await reuse_failure(page, cached, option="Raport zbiorczy") == "option_missing"


async def test_reuse_failure_without_an_option_keeps_ignoring_the_option_list(page):
    """The `render`/`compile` callers reach this through `reuse_is_valid`, which
    validates a `CachedAction` and so has no label to pass. Omitting the option
    has to stay exactly as permissive as it was — the change is additive or it
    silently breaks every non-guide reuse of a select.
    """

    await page.set_content(
        '<select aria-label="Rodzaj raportu">'
        "<option>Raport jednostki</option><option>Raport autora</option>"
        "</select>"
    )
    target = RoleTarget(role="combobox", name="Rodzaj raportu")
    identity = await capture_identity(await build_locator(page, target))
    cached = _cached(target, identity, action="select")

    assert await reuse_failure(page, cached) is None
    assert await reuse_is_valid(page, cached) is True


async def test_reuse_failure_accepts_the_option_that_is_present(page):
    """The forwarded option must not become a new way for a good target to fail."""

    await page.set_content(
        '<select aria-label="Rodzaj raportu">'
        "<option>Raport jednostki</option><option>Raport autora</option>"
        "</select>"
    )
    target = RoleTarget(role="combobox", name="Rodzaj raportu")
    identity = await capture_identity(await build_locator(page, target))
    cached = _cached(target, identity, action="select")

    assert await reuse_failure(page, cached, option="Raport autora") is None


async def test_reuse_failure_returns_sensitive_target_for_teach_type_on_password_field(page):
    await page.set_content('<label for="value">Access</label><input id="value" type="password">')
    target = LabelTarget(label="Access")
    identity = await capture_identity(await build_locator(page, target))
    cached = CachedAction(
        action="type",
        target=target,
        identity=identity,
        expect="none",
        input_text="hunter2",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="enter hunter2 into the field",
            expect="none",
            config_hash="config",
        ),
    )

    assert await reuse_failure(page, cached) == "sensitive_target"


async def test_reuse_failure_hidden_wait_regression_returns_none_without_identity_check(page):
    # Element present, visible, and unique (count() == 1) — the same DOM shape
    # as test_wait_for_hidden_cache_without_identity_can_be_reused, but this
    # test asserts on reuse_failure() directly instead of through the
    # reuse_is_valid() boolean wrapper.
    #
    # The count() == 1 shape is what actually exercises the regression this
    # test is named for: a buggy implementation that dropped the early
    # ``return`` from the "hidden" branch and fell through to
    # validate_compile_time() would find a visible, unique match
    # (ValidationOk), then hit ``if cached.identity is None: return
    # "identity_missing"`` in reuse_failure() — a hidden wait's cached entry
    # never carries an identity by design. A DOM with count() == 0 would
    # *not* prove this: validate_compile_time() returns
    # ValidationFail("not_found", ...) before any identity check is reached,
    # so a fall-through bug would surface as "not_found" instead, without
    # ever exercising the identity_missing branch this test targets.
    await page.set_content('<div data-testid="spinner">Ładowanie</div>')
    cached = _cached(
        ByTestidTarget(testid="spinner"),
        None,
        action="waitFor",
        state="hidden",
    )

    assert await reuse_failure(page, cached) is None


async def test_reuse_is_valid_still_returns_bool_not_reason(page):
    await page.set_content("<main><button>Zaloguj</button></main>")
    cached = _cached(
        RoleTarget(role="button", name="Zaloguj"),
        Identity(tag="a", ancestry_digest="not-the-current-digest"),
    )

    result = await reuse_is_valid(page, cached)

    assert result is False
