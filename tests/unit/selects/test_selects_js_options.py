"""Option rows: optgroups, disabled rows, label resolution, scrolling into view.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map. The through-line is that the on-camera rows must agree with what
``locator.select_option(label=…)`` would pick: compile resolves an option label
through Playwright and render through this widget, and one scenario must not get
two different answers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import _TWO_FRAMES, _inject, _options, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


async def test_optgroups_are_headings_and_disabled_options_are_not_clickable(page: Page) -> None:
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<optgroup label='Północ'><option>Gdańsk</option><option disabled>Olsztyn</option></optgroup>"
        "<optgroup label='Południe'><option>Kraków</option></optgroup>"
        "</select></body>"
    )
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    shape = await page.evaluate(
        """() => {
      const list = window.__guidebot_selects.listFor(document.getElementById('s'));
      return {
        groups: Array.from(list.querySelectorAll('[data-guidebot-optgroup]')).map((n) => n.textContent),
        groupsClickable: Array.from(list.querySelectorAll('[data-guidebot-optgroup]'))
          .some((n) => n.hasAttribute('data-guidebot-option-index')),
        rows: Array.from(list.querySelectorAll('[data-guidebot-option-index]'))
          .map((n) => [n.getAttribute('data-guidebot-option-index'), n.textContent]),
        disabledDimmed: Number.parseFloat(getComputedStyle(
          list.querySelector('[data-guidebot-option-index="1"]')).opacity) < 1,
      };
    }"""
    )
    assert shape["groups"] == ["Północ", "Południe"]
    assert shape["groupsClickable"] is False
    assert shape["rows"] == [["0", "Gdańsk"], ["1", "Olsztyn"], ["2", "Kraków"]]
    assert shape["disabledDimmed"] is True

    # Clicking the disabled row changes nothing and does not close the list.
    await page.evaluate(
        """() => {
      window.__events = [];
      document.getElementById('s').addEventListener('change', () => window.__events.push('change'));
      window.__guidebot_selects.listFor(document.getElementById('s'))
        .querySelector('[data-guidebot-option-index="1"]')
        .dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }"""
    )
    state = await page.evaluate(
        "() => ({events: window.__events, index: document.getElementById('s').selectedIndex})"
    )
    assert state == {"events": [], "index": 0}


async def test_option_index_for_normalizes_whitespace_but_matches_case_exactly(
    page: Page,
) -> None:
    """Whitespace is collapsed; case is not.

    The case-insensitive fallback lived here and nowhere else: compile resolves
    through Playwright's `select_option(label=…)` and the listbox path through
    `_OPTION_INDEX_JS`, both exact. One scenario, one option label — and three
    different answers depending on the control's shape. A label that differs
    only in case must now miss on every path, which is what compile would have
    said all along.
    """

    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<option>Pierwszy</option>"
        "<option>  Drugi\n   wybór </option>"
        "<option>Trzeci</option>"
        "</select></body>"
    )
    await _inject(page)
    found = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      return [
        api.optionIndexFor(s, 'Drugi wybór'),
        api.optionIndexFor(s, '   Drugi    wybór  '),
        api.optionIndexFor(s, 'drugi WYBÓR'),
        api.optionIndexFor(s, 'Pierwszy'),
        api.optionIndexFor(s, 'Nie ma takiej'),
      ];
    }"""
    )
    assert found == [1, 1, -1, 0, -1]


async def test_the_label_attribute_wins_over_the_option_text_like_select_option_does(
    page: Page,
) -> None:
    """M8: compile drives via `locator.select_option(label=…)`, which reads `option.label`.

    Resolving the row text or the index off `textContent` would make compile and
    render disagree about the same option, and put text on camera that the native
    control never shows.
    """
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<option>Pierwszy</option>"
        "<option label='Krótko'>bardzo długi tekst opcji</option>"
        "</select></body>"
    )
    await _inject(page)
    # Exactly what compile does — the reference behaviour render must match.
    await page.select_option("#s", label="Krótko")
    await page.evaluate(_TWO_FRAMES)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      const list = api.listFor(s);
      return {
        selectedIndex: s.selectedIndex,
        indexForLabel: api.optionIndexFor(s, 'Krótko'),
        indexForText: api.optionIndexFor(s, 'bardzo długi tekst opcji'),
        rowText: list.querySelector('[data-guidebot-option-index="1"]').textContent,
        buttonText: api.buttonFor(s).textContent.trim(),
      };
    }"""
    )
    assert state["selectedIndex"] == 1
    assert state["indexForLabel"] == 1, "optionIndexFor ignored the label attribute"
    assert state["indexForText"] == -1, "textContent resolved an option select_option would not"
    assert state["rowText"] == "Krótko", "the on-camera row text differs from the native control"
    assert state["buttonText"] == "Krótko"


async def test_scroll_option_into_view_brings_a_far_row_into_the_list_box(page: Page) -> None:
    labels = [f"Opcja {i}" for i in range(40)]
    await page.set_content(
        f"<body style='margin:0'><select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      const list = api.listFor(s);
      const before = list.scrollTop;
      api.scrollOptionIntoView(s, 35);
      const row = list.querySelector('[data-guidebot-option-index="35"]');
      const lr = list.getBoundingClientRect();
      const rr = row.getBoundingClientRect();
      return {before: before, after: list.scrollTop,
              inside: rr.top >= lr.top - 1 && rr.bottom <= lr.bottom + 1};
    }"""
    )
    assert state["before"] == 0
    assert state["after"] > 0
    assert state["inside"] is True
