"""Reguła etykiety opcji z walidacji JEST regułą etykiety z wykonania.

Jeden test, własny plik — bo jako jedyny w całym zestawie walidacji sięga poza
własny pakiet: importuje `_OPTION_INDEX_JS` z `guidebot_recorder.recorder.select._js`,
żeby porównać werdykty walidatora (resolver) i ścieżki wykonania (recorder) na
tym samym korpusie. To celowe przecięcie granicy pakietu, nie przypadek —
gdyby zniknęło, trzy niezależne implementacje jednej reguły znów mogłyby się
rozjechać niezauważone. Szczegóły w docstringu testu.

Reszta walidacji kompilacyjnej: `test_validate_compile_time.py`.
"""

import json
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from guidebot_recorder.recorder.select._js import _OPTION_INDEX_JS
from guidebot_recorder.resolver.validate import (
    _offers_option,
    _select_option_labels,
)
from guidebot_recorder.selects.visibility import shape_prelude

from ._validate_page import playwright_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with playwright_page() as browser_page:
        yield browser_page


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
