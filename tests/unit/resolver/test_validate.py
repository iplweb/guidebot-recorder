import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    TestidTarget as ByTestidTarget,
    TextTarget,
)
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    build_locator,
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

    result = await validate_compile_time(
        page, RoleTarget(role="button", name="Zaloguj"), "click"
    )

    assert isinstance(result, ValidationOk)
    assert await result.locator.count() == 1


async def test_validate_compile_time_rejects_ambiguous_substring_match(page):
    await page.set_content(
        "<button>Zaloguj</button><button>Zaloguj jako administrator</button>"
    )

    result = await validate_compile_time(
        page,
        RoleTarget(role="button", name="Zaloguj", exact=False),
        "click",
    )

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_unique"


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
            # display:none jest wykluczony z drzewa dostępności → get_by_role
            # nie trafia w element → not_found (nie not_visible)
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
async def test_validate_compile_time_checks_action_requirements(
    page, html, target, action, reason
):
    await page.set_content(html)

    result = await validate_compile_time(page, target, action)

    assert isinstance(result, ValidationFail)
    assert result.reason == reason


async def test_validate_compile_time_accepts_editable_textbox_for_type(page):
    await page.set_content('<label for="name">Imię</label><input id="name">')

    result = await validate_compile_time(
        page, LabelTarget(label="Imię"), "type"
    )

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_rejects_readonly_textbox_for_type(page):
    await page.set_content('<input data-testid="locked" value="x" readonly>')

    result = await validate_compile_time(
        page, ByTestidTarget(testid="locked"), "type"
    )

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

    assert await reuse_is_valid(
        page, _cached(target, identity, action="waitFor")
    ) is False
