import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    TextTarget,
)
from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,
)
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    build_locator,
    is_sensitive_type_target,
    reuse_failure,
    reuse_is_valid,
    validate_compile_time,
)


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


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


async def test_validate_compile_time_accepts_one_visible_enabled_match(page):
    await page.set_content("<button>Zaloguj</button>")

    result = await validate_compile_time(page, RoleTarget(role="button", name="Zaloguj"), "click")

    assert isinstance(result, ValidationOk)
    assert await result.locator.count() == 1


async def test_validate_compile_time_rejects_ambiguous_substring_match(page):
    await page.set_content("<button>Zaloguj</button><button>Zaloguj jako administrator</button>")

    result = await validate_compile_time(
        page,
        RoleTarget(role="button", name="Zaloguj", exact=False),
        "click",
    )

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_unique"


async def test_validate_compile_time_select_accepts_native_select(page):
    await page.set_content(
        '<select aria-label="Rodzaj"><option>lista</option><option>tabela</option></select>'
    )

    result = await validate_compile_time(page, RoleTarget(role="combobox", name="Rodzaj"), "select")

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_select_rejects_non_native_combobox(page):
    await page.set_content('<div role="combobox" aria-label="Rodzaj" tabindex="0">lista</div>')

    result = await validate_compile_time(page, RoleTarget(role="combobox", name="Rodzaj"), "select")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_select"


async def test_validate_compile_time_select_rejects_a_dropdown_without_the_wanted_option(page):
    """The wrong-but-plausible combobox must fail here, not time out in execution."""

    await page.set_content(
        '<select aria-label="Rodzaj raportu">'
        "<option>Raport jednostki</option><option>Raport autora</option>"
        "</select>"
    )

    result = await validate_compile_time(
        page,
        RoleTarget(role="combobox", name="Rodzaj raportu"),
        "select",
        option="Artykuł w czasopismie",
    )

    assert isinstance(result, ValidationFail)
    assert result.reason == "option_missing"
    # the message has to carry both halves of the mismatch for a useful re-prompt
    assert "Artykuł w czasopismie" in result.message
    assert "Raport jednostki" in result.message
    assert "Raport autora" in result.message


async def test_validate_compile_time_select_accepts_option_despite_whitespace_and_case(page):
    """Validation must never be stricter than ``Recorder._step_option_visibly``."""

    await page.set_content(
        '<select aria-label="Charakter">\n'
        "  <option>\n    Artykuł   w\n    czasopismie\n  </option>\n"
        "  <option>Rozdział</option>\n"
        "</select>"
    )

    result = await validate_compile_time(
        page,
        RoleTarget(role="combobox", name="Charakter"),
        "select",
        option="  artykuł W czasopismie ",
    )

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_select_without_an_option_keeps_checking_only_the_element(page):
    """``reuse_is_valid`` has no option to pass; that path must not start failing."""

    await page.set_content('<select aria-label="Rodzaj"><option>lista</option></select>')

    result = await validate_compile_time(page, RoleTarget(role="combobox", name="Rodzaj"), "select")

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_reports_an_empty_select_as_option_missing(page):
    await page.set_content('<select aria-label="Rodzaj"></select>')

    result = await validate_compile_time(
        page, RoleTarget(role="combobox", name="Rodzaj"), "select", option="cokolwiek"
    )

    assert isinstance(result, ValidationFail)
    assert result.reason == "option_missing"


async def test_validate_compile_time_ignores_the_option_for_non_select_actions(page):
    """An option label is meaningless for a click and must not be checked."""

    await page.set_content("<button>Zaloguj</button>")

    result = await validate_compile_time(
        page, RoleTarget(role="button", name="Zaloguj"), "click", option="nie ma takiej opcji"
    )

    assert isinstance(result, ValidationOk)


async def test_build_locator_supports_all_structural_strategies(page):
    await page.set_content(
        """
        <label for="email">E-mail</label><input id="email" value="visible-value">
        <p>Gotowe</p>
        <button data-testid="save">Zapisz</button>
        """
    )

    assert await (await build_locator(page, TextTarget(text="Gotowe"))).count() == 1
    assert await (await build_locator(page, LabelTarget(label="E-mail"))).count() == 1
    assert await (await build_locator(page, ByTestidTarget(testid="save"))).count() == 1


async def test_build_locator_applies_recursive_scope_and_nth(page):
    await page.set_content(
        """
        <button>Wybierz</button>
        <section data-testid="dialog">
          <button>Wybierz</button><button>Wybierz</button>
        </section>
        """
    )
    target = RoleTarget(
        role="button",
        name="Wybierz",
        nth=1,
        scope=ByTestidTarget(testid="dialog"),
    )

    locator = await build_locator(page, target)

    assert await locator.count() == 1
    assert await locator.evaluate("element => element.parentElement.dataset.testid") == "dialog"


@pytest.mark.parametrize(
    ("html", "target", "action", "reason"),
    [
        (
            "<button disabled>Zapisz</button>",
            RoleTarget(role="button", name="Zapisz"),
            "click",
            "not_enabled",
        ),
        (
            # display:none is excluded from the accessibility tree → get_by_role
            # does not match the element → not_found (not not_visible)
            "<button style='display:none'>Zapisz</button>",
            RoleTarget(role="button", name="Zapisz"),
            "click",
            "not_found",
        ),
        (
            "<button>Nie pole tekstowe</button>",
            RoleTarget(role="button", name="Nie pole tekstowe"),
            "type",
            "incompatible_type",
        ),
    ],
)
async def test_validate_compile_time_checks_action_requirements(page, html, target, action, reason):
    await page.set_content(html)

    result = await validate_compile_time(page, target, action)

    assert isinstance(result, ValidationFail)
    assert result.reason == reason


async def test_validate_compile_time_accepts_editable_textbox_for_type(page):
    await page.set_content('<label for="name">Imię</label><input id="name">')

    result = await validate_compile_time(page, LabelTarget(label="Imię"), "type")

    assert isinstance(result, ValidationOk)


@pytest.mark.parametrize(
    "html",
    [
        '<label for="value">Access</label><input id="value" type="password">',
        '<label for="value">Access</label><input id="value" autocomplete="one-time-code">',
        '<label for="value">Passcode</label><input id="value">',
    ],
)
async def test_sensitive_type_target_detects_secret_field_metadata(page, html):
    await page.set_content(html)
    result = await validate_compile_time(page, LabelTarget(label="Access"), "type")
    if "Passcode" in html:
        result = await validate_compile_time(page, LabelTarget(label="Passcode"), "type")

    assert isinstance(result, ValidationOk)
    assert await is_sensitive_type_target(result.locator) is True


async def test_sensitive_type_target_accepts_regular_email_field(page):
    await page.set_content('<label for="value">E-mail</label><input id="value" type="email">')
    result = await validate_compile_time(page, LabelTarget(label="E-mail"), "type")

    assert isinstance(result, ValidationOk)
    assert await is_sensitive_type_target(result.locator) is False


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


async def test_validate_compile_time_rejects_readonly_textbox_for_type(page):
    await page.set_content('<input data-testid="locked" value="x" readonly>')

    result = await validate_compile_time(page, ByTestidTarget(testid="locked"), "type")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_editable"


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
