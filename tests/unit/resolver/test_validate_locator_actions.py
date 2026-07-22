"""`build_locator` oraz wymagania akcji wobec znalezionego elementu.

Dwie warstwy, które w kodzie stykają się bezpośrednio: najpierw z celu powstaje
lokator (strategie strukturalne, zakres, `nth`), a potem walidacja pyta, czy tak
znaleziony element da się kliknąć / w niego napisać. Tu leżą też testy wykrywania
pola wrażliwego (`is_sensitive_type_target`) — pytanie zadawane o ten sam,
świeżo zwalidowany lokator.

Odpowiadające im testy odmowy przy odtwarzaniu z cache (`sensitive_target`)
mieszkają w `test_validate_reuse.py` — tam pyta o to samo `reuse_failure`.
"""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    TextTarget,
)
from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,
)
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    build_locator,
    is_sensitive_type_target,
    validate_compile_time,
)

from ._validate_page import playwright_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with playwright_page() as browser_page:
        yield browser_page


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


async def test_validate_compile_time_rejects_readonly_textbox_for_type(page):
    await page.set_content('<input data-testid="locked" value="x" readonly>')

    result = await validate_compile_time(page, ByTestidTarget(testid="locked"), "type")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_editable"
