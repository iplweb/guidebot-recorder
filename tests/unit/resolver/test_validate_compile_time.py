"""`validate_compile_time` — czy ten cel w ogóle nadaje się pod tę akcję.

Jeden plik na jedno pytanie: istnienie, unikalność, widoczność, zgodność akcji
z elementem oraz — dla `select` — obecność żądanej opcji. Zbite w całość, bo
wszystkie te odpowiedzi wychodzą z tej samej funkcji i psują się wspólnie:
poluzowanie widoczności dla `select` musi jednocześnie zostać sprawdzone jako
NIE poluzowane dla `click`.

Testy ze zwrotem `option_missing` na trzech klasach kontrolek siedzą tu razem
z resztą reguły opcji — trzy przypadki mają sens wyłącznie względem siebie.

Pokrewne pliki: `test_validate_option_rule.py` (zgodność reguły etykiety
z wykonaniem), `test_validate_locator_actions.py` (budowa lokatora i wymagania
akcji), `test_validate_reuse.py` (`reuse_is_valid` / `reuse_failure`).
"""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,
)
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    validate_compile_time,
)

from ._validate_page import playwright_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with playwright_page() as browser_page:
        yield browser_page


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


async def test_validate_compile_time_select_accepts_option_despite_whitespace(page):
    """Whitespace is collapsed on both sides — the one thing the rule forgives."""

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
        option="  Artykuł   w czasopismie ",
    )

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_select_rejects_an_option_differing_only_in_case(page):
    """Validation is exact because every execution path is; looser would fail later.

    The comparison used to fall back to a case-insensitive pass, mirroring a
    case-insensitive fallback the shim then dropped. Left as it was, a label
    differing only in case would pass validation, be frozen as the resolved
    target, and fail during playback — precisely the late failure ``option_missing``
    exists to remove.
    """

    await page.set_content(
        '<select aria-label="Charakter"><option>Artykuł w czasopiśmie</option></select>'
    )

    result = await validate_compile_time(
        page,
        RoleTarget(role="combobox", name="Charakter"),
        "select",
        option="artykuł W czasopiśmie",
    )

    assert isinstance(result, ValidationFail)
    assert result.reason == "option_missing"


async def test_validate_compile_time_select_accepts_hidden_select_with_visible_widget(page):
    # Tom-Select-style hiding: the original <select> is display:none, but a
    # widget the select's aria-controls names is on screen. §6's relaxation
    # exists exactly for this case.
    await page.set_content(
        """
        <select data-testid="province" style="display:none" aria-controls="widget">
          <option>Mazowieckie</option>
        </select>
        <div id="widget" style="width:200px;height:30px;">Mazowieckie</div>
        """
    )

    result = await validate_compile_time(page, ByTestidTarget(testid="province"), "select")

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_select_rejects_hidden_select_without_a_widget(page):
    await page.set_content(
        '<select data-testid="province" style="display:none"><option>Mazowieckie</option></select>'
    )

    result = await validate_compile_time(page, ByTestidTarget(testid="province"), "select")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_visible"


async def test_validate_compile_time_click_does_not_get_the_select_relaxation(page):
    # The exact same DOM as the accepted `select` case above — a hidden
    # <select> with a perfectly visible associated widget — but for any other
    # action the generic "is_visible()" gate must still apply unchanged. The
    # relaxation is `select`-only and must not leak.
    await page.set_content(
        """
        <select data-testid="province" style="display:none" aria-controls="widget">
          <option>Mazowieckie</option>
        </select>
        <div id="widget" style="width:200px;height:30px;">Mazowieckie</div>
        """
    )

    result = await validate_compile_time(page, ByTestidTarget(testid="province"), "click")

    assert isinstance(result, ValidationFail)
    assert result.reason == "not_visible"


async def test_validate_compile_time_select_without_an_option_keeps_checking_only_the_element(page):
    """``reuse_is_valid`` has no option to pass; that path must not start failing."""

    await page.set_content('<select aria-label="Rodzaj"><option>lista</option></select>')

    result = await validate_compile_time(page, RoleTarget(role="combobox", name="Rodzaj"), "select")

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_reports_an_empty_select_as_option_missing(page):
    await page.set_content('<select aria-label="Rodzaj" style="width:200px;height:24px"></select>')

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


# --- `option_missing` against the three control classes --------------------
#
# The check reads ``select.options``, so it is meaningful exactly where
# execution reads them too. Two of the three classes qualify; the third does
# not, and the difference is decided by the one shared "is this select already
# enhanced?" predicate, never by a rule restated here.

#: select2's pattern: the original is kept but clipped to 1x1 px, and the
#: widget the viewer actually operates sits next to it. Options arrive over
#: AJAX only once the widget is opened, so the original is empty at compile
#: time — and that says nothing at all about whether this is the right target.
_AJAX_ENHANCED = """
    <select data-testid="autor" id="sel" aria-controls="widget"
            style="width:1px;height:1px;position:absolute;overflow:hidden"></select>
    <div id="widget" style="width:200px;height:30px">Wybierz autora</div>
"""


async def test_validate_compile_time_skips_the_option_check_for_an_ajax_enhanced_widget(page):
    """A control the branch can genuinely drive must not start failing validation.

    The render drives an enhanced widget through the *page's* DOM list — beat 2
    clicks the node that appeared after opening whose text equals the label. The
    hidden original's (here empty) option set is not what execution searches, so
    it cannot be evidence that the resolver picked the wrong element.
    """

    await page.set_content(_AJAX_ENHANCED)

    result = await validate_compile_time(
        page, ByTestidTarget(testid="autor"), "select", option="Kowalski, Jan"
    )

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_skips_the_option_check_for_a_marker_class_widget(page):
    """The marker-class half of the predicate exempts a full-size original too.

    A ``select2-hidden-accessible`` select that still has a box validates (so the
    author can reach for ``mode: native``), and the page owns its list either
    way — so its own options are not the list execution will search.
    """

    await page.set_content(
        '<select aria-label="Autor" class="select2-hidden-accessible" '
        'style="width:200px;height:24px"><option>Nowak, Anna</option></select>'
    )

    result = await validate_compile_time(
        page, RoleTarget(role="combobox", name="Autor"), "select", option="Kowalski, Jan"
    )

    assert isinstance(result, ValidationOk)


async def test_validate_compile_time_checks_the_option_on_a_natively_visible_listbox(page):
    """A ``multiple`` listbox is driven off ``select.options``, so it is checked.

    Its rows are the very ``<option>`` elements ``_OPTION_INDEX_JS`` addresses,
    so an absent label is a real defect — and rejecting it here beats clicking a
    row that does not exist, on camera, minutes into a render.
    """

    await page.set_content(
        '<select aria-label="Jednostki" multiple size="3">'
        "<option>Katedra A</option><option>Katedra B</option></select>"
    )
    target = RoleTarget(role="listbox", name="Jednostki")

    missing = await validate_compile_time(page, target, "select", option="Katedra C")
    present = await validate_compile_time(page, target, "select", option="Katedra B")

    assert isinstance(missing, ValidationFail)
    assert missing.reason == "option_missing"
    assert isinstance(present, ValidationOk)
