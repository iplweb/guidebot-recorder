import json
from importlib.resources import files

import pytest
from playwright.async_api import Error as PlaywrightError
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
from guidebot_recorder.recorder.recorder import _OPTION_INDEX_JS
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    _offers_option,
    _select_option_labels,
    build_locator,
    is_sensitive_type_target,
    reuse_is_valid,
    validate_compile_time,
)
from guidebot_recorder.selects.visibility import shape_prelude


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


# --- validation's label rule is execution's label rule ---------------------

_SELECTS_JS = shape_prelude() + files("guidebot_recorder.selects").joinpath("selects.js").read_text(
    "utf-8"
)

#: Options carrying no ``label`` attribute, so ``HTMLOptionElement.label`` is
#: already the whitespace-collapsed text and Playwright's own matcher can join
#: the comparison without a rule of its own entering the picture.
_CORPUS_OPTIONS = ["Artykuł w czasopiśmie", "Rozdział", "\n  Raport   jednostki\n"]

#: (wanted label, whether every rule must accept it)
_CORPUS_WANTED = [
    ("Artykuł w czasopiśmie", True),
    ("Artykuł  w   czasopiśmie", True),  # whitespace is collapsed on both sides
    ("  Rozdział ", True),  # …including leading and trailing
    ("Raport jednostki", True),  # collapsed in the DOM, not in the scenario
    ("artykuł w czasopiśmie", False),  # case is significant — the unified rule
    ("ROZDZIAŁ", False),
    ("Raport", False),  # never a prefix or substring match
    ("", False),
]


async def test_validate_option_rule_matches_execution(page):
    """Validation and every execution path answer the same label question.

    The three rules are separate implementations of one rule (design §7: exact
    after whitespace collapsing, everywhere):

    * ``_offers_option`` — compile-time validation, here;
    * ``_OPTION_INDEX_JS`` — the natively-visible listbox path in the recorder;
    * ``optionIndexFor`` in ``selects.js`` — the shim's option rows.

    They drifted once already, in opposite directions, and the drift is invisible
    in a diff because the three live in three files. Validation being the *looser*
    of them is the dangerous direction: a label differing only in case would pass
    here and then fail during playback, which is the late failure ``option_missing``
    was added to prevent. This pins them together over a corpus that exercises
    exactly the axes they disagreed on — whitespace and case.
    """

    await page.set_content(
        f'<select aria-label="Charakter" style="width:240px">'
        f"{''.join(f'<option>{label}</option>' for label in _CORPUS_OPTIONS)}"
        f"</select>"
    )
    await page.evaluate("window.__guidebot_selects_config = {settleMs: 20};")
    await page.evaluate(_SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")

    locator = page.locator("select")
    labels = await _select_option_labels(locator)

    for wanted, expected in _CORPUS_WANTED:
        validation = _offers_option(labels, wanted)
        listbox = await locator.evaluate(_OPTION_INDEX_JS, wanted) >= 0
        shim = (
            await locator.evaluate(
                "(el, label) => window.__guidebot_selects.optionIndexFor(el, label)", wanted
            )
            >= 0
        )
        try:
            await locator.select_option(label=wanted, timeout=500)
            playwright_direct = True
        except PlaywrightError:
            playwright_direct = False

        verdicts = {
            "validation": validation,
            "listbox": listbox,
            "shim": shim,
            "select_option": playwright_direct,
        }
        assert verdicts == dict.fromkeys(verdicts, expected), (
            f"the label rules disagree on {wanted!r}: {json.dumps(verdicts)}"
        )


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
