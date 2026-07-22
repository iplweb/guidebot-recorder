"""What the shim installs, and how it behaves when it is driven.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map and for why it keeps the unsuffixed name. Everything here takes the
structural invariant asserted there for granted and asks the next question:
given that the page DOM is untouched, what does the overlay do instead?

Covered: the button/list pair and its uid addressing, pinning to the select's
rect, the copied metrics, which selects are left alone, opening via mousedown /
keyboard / a real click, choosing an option, and the list's height clamp.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import NESTED, _inject, _options, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


# Must stay strictly below the cursor's own z-index (overlay/cursor.js:18), so
# the synthetic cursor is never painted underneath the option list.
MAX_Z_INDEX = 2147483647


async def test_raw_select_gets_button_and_list_addressed_by_uid(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    info = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const uid = s.getAttribute('data-guidebot-shimmed');
      const button = document.querySelector('[data-guidebot-select-button][data-guidebot-for="' + uid + '"]');
      const list = document.querySelector('[data-guidebot-select-list][data-guidebot-for="' + uid + '"]');
      return {
        uid: uid,
        buttonMatches: button === api.buttonFor(s),
        listMatches: list === api.listFor(s),
        buttonAria: button.getAttribute('aria-hidden'),
        listAria: list.getAttribute('aria-hidden'),
        pointerEvents: getComputedStyle(button).pointerEvents,
        buttonZ: Number(getComputedStyle(button).zIndex),
        listZ: Number(getComputedStyle(list).zIndex),
        listDisplay: getComputedStyle(list).display,
        buttonPosition: getComputedStyle(button).position,
        listPosition: getComputedStyle(list).position,
      };
    }"""
    )
    assert info["uid"]
    assert info["buttonMatches"] and info["listMatches"]
    assert info["buttonAria"] == "true" and info["listAria"] == "true"
    # The real <select> must remain Playwright's hit target.
    assert info["pointerEvents"] == "none"
    assert 0 < info["buttonZ"] < MAX_Z_INDEX
    assert 0 < info["listZ"] < MAX_Z_INDEX
    assert info["listDisplay"] == "none", "the list must start closed"
    assert info["buttonPosition"] == "fixed" and info["listPosition"] == "fixed"


async def test_button_is_pinned_to_the_selects_rect(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    delta = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const a = s.getBoundingClientRect();
      const b = window.__guidebot_selects.buttonFor(s).getBoundingClientRect();
      return [Math.abs(a.left - b.left), Math.abs(a.top - b.top),
              Math.abs(a.width - b.width), Math.abs(a.height - b.height)];
    }"""
    )
    assert max(delta) < 1.0, delta


async def test_button_copies_the_selects_own_metrics_and_only_falls_back_when_transparent(
    page: Page,
) -> None:
    """A transparent control would leave the button see-through on camera.

    Only the alpha channel may trigger the fallback: `rgb(255, 255, 0)` is an
    opaque yellow and must be copied verbatim.
    """
    await page.set_content(
        "<body style='margin:0'>"
        "<select id='a' style='width:200px;background-color:rgb(255,255,0);"
        "font-family:Georgia;font-size:19px'><option>x</option></select>"
        "<select id='b' style='width:200px;background-color:rgba(0,0,0,0)'>"
        "<option>x</option></select>"
        "</body>"
    )
    await _inject(page)
    styles = await page.evaluate(
        """() => {
      const api = window.__guidebot_selects;
      const read = (id) => {
        const cs = getComputedStyle(api.buttonFor(document.getElementById(id)));
        return [cs.backgroundColor, cs.fontFamily, cs.fontSize];
      };
      return {a: read('a'), b: read('b')};
    }"""
    )
    assert styles["a"] == ["rgb(255, 255, 0)", "Georgia", "19px"]
    assert styles["b"][0] == "rgb(255, 255, 255)"


LEFT_ALONE = {
    "select2_clipped_to_1px": (
        "<select id='s' style=\"position:absolute;width:1px;height:1px;padding:0;"
        'margin:-1px;overflow:hidden;clip:rect(0 0 0 0)">' + _options(["a", "b"]) + "</select>"
    ),
    "display_none": ("<select id='s' style='display:none'>" + _options(["a", "b"]) + "</select>"),
    "marker_class_select2": (
        "<select id='s' class='select2-hidden-accessible' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "marker_class_tomselect": (
        "<select id='s' class='tomselected' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "marker_class_chosen": (
        "<select id='s' class='chosen-select' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "multiple": (
        "<select id='s' multiple style='width:200px;height:80px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "size_gt_1": (
        "<select id='s' size='4' style='width:200px;height:80px'>"
        + _options(["a", "b", "c", "d"])
        + "</select>"
    ),
}


@pytest.mark.parametrize("html", list(LEFT_ALONE.values()), ids=list(LEFT_ALONE))
async def test_non_raw_selects_are_left_alone(page: Page, html: str) -> None:
    await page.set_content(f"<body style='margin:0'><div id='host'>{html}</div></body>")
    await _inject(page)
    state = await page.evaluate(
        """() => ({
      overlays: document.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]').length,
      marker: document.getElementById('s').hasAttribute('data-guidebot-shimmed'),
      isShimmed: window.__guidebot_selects.isShimmed(document.getElementById('s')),
      buttonFor: window.__guidebot_selects.buttonFor(document.getElementById('s')),
    })"""
    )
    assert state == {"overlays": 0, "marker": False, "isShimmed": False, "buttonFor": None}


async def test_mousedown_opens_the_list_and_suppresses_the_native_popup(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    result = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const ev = new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0});
      s.dispatchEvent(ev);
      const list = window.__guidebot_selects.listFor(s);
      return {prevented: ev.defaultPrevented, display: getComputedStyle(list).display};
    }"""
    )
    assert result["prevented"] is True, "native OS popup was not suppressed"
    assert result["display"] != "none", "mousedown did not open the DOM list"


@pytest.mark.parametrize("key", ["ArrowDown", "Enter", " "])
async def test_keyboard_opens_the_list(page: Page, key: str) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    result = await page.evaluate(
        """(key) => {
      const s = document.getElementById('s');
      const ev = new KeyboardEvent('keydown', {key: key, bubbles: true, cancelable: true});
      s.dispatchEvent(ev);
      return {
        prevented: ev.defaultPrevented,
        display: getComputedStyle(window.__guidebot_selects.listFor(s)).display,
      };
    }""",
        key,
    )
    assert result["display"] != "none"
    # Same reason as mousedown: unprevented, the key would also open Chromium's
    # native, un-recordable popup (and ' ' would scroll the page).
    assert result["prevented"] is True, "the native key handling was not suppressed"


async def test_real_click_on_the_select_unfurls_the_list(page: Page) -> None:
    """End-to-end proof that the shim button does not steal the hit target."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.click("#s")
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    list_selector = f'[data-guidebot-select-list][data-guidebot-for="{uid}"]'
    await page.wait_for_selector(list_selector, state="visible", timeout=3000)


# Two shimmed selects on one page — the normal case, not an edge case. The bare
# `[data-guidebot-option-index="N"]` selector then matches one row per select.
NESTED_AND_A_SECOND_SELECT = NESTED.replace(
    "</main></body>",
    "</main><select id='other' style='width:220px'>"
    "<option>Mazowieckie</option><option>Lubelskie</option><option>Śląskie</option>"
    "</select></body>",
)


def option_selector(uid: str, index: int) -> str:
    """The documented, uid-scoped way to address one shimmed select's option row.

    The bare attribute selector is ambiguous the moment a page has two shimmed
    selects, and Playwright's strict mode rejects it.
    """
    return (
        f'[data-guidebot-select-list][data-guidebot-for="{uid}"] '
        f'[data-guidebot-option-index="{index}"]'
    )


async def test_option_rows_must_be_addressed_scoped_to_their_own_select(page: Page) -> None:
    """M6: the bare selector is a Playwright strict-mode violation."""
    await page.set_content(NESTED_AND_A_SECOND_SELECT)
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    assert await page.locator('[data-guidebot-option-index="2"]').count() == 2
    with pytest.raises(Exception, match="strict mode violation"):
        await page.locator('[data-guidebot-option-index="2"]').click(timeout=1500)
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    assert await page.locator(option_selector(uid, 2)).count() == 1
    await page.locator(option_selector(uid, 2)).click()
    assert await page.evaluate("() => document.getElementById('s').selectedIndex") == 2


async def test_choosing_an_option_sets_value_and_fires_input_and_change(page: Page) -> None:
    await page.set_content(NESTED_AND_A_SECOND_SELECT)
    await _inject(page)
    await page.evaluate(
        """() => {
      window.__events = [];
      const s = document.getElementById('s');
      for (const type of ['input', 'change']) {
        s.addEventListener(type, (e) => window.__events.push([type, e.bubbles]));
      }
      window.__guidebot_selects.open(s);
    }"""
    )
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.click(option_selector(uid, 2))
    state = await page.evaluate(
        """() => ({
      value: document.getElementById('s').value,
      index: document.getElementById('s').selectedIndex,
      otherIndex: document.getElementById('other').selectedIndex,
      events: window.__events,
      display: getComputedStyle(
        window.__guidebot_selects.listFor(document.getElementById('s'))).display,
    })"""
    )
    assert state["index"] == 2
    assert state["value"] == "Śląskie"
    assert state["otherIndex"] == 0, "the click leaked into the other shimmed select"
    assert state["events"] == [["input", True], ["change", True]]
    assert state["display"] == "none", "choosing an option must close the list"


async def test_rechoosing_the_current_option_fires_nothing(page: Page) -> None:
    """Native selects stay silent when the value does not change (spec §4)."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      s.selectedIndex = 1;
      window.__events = [];
      for (const type of ['input', 'change']) {
        s.addEventListener(type, () => window.__events.push(type));
      }
      window.__guidebot_selects.open(s);
    }"""
    )
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.click(option_selector(uid, 1))
    state = await page.evaluate(
        "() => ({events: window.__events, index: document.getElementById('s').selectedIndex})"
    )
    assert state["events"] == []
    assert state["index"] == 1


async def test_list_opens_downward_and_clamps_max_height_with_internal_scrolling(
    page: Page,
) -> None:
    labels = [f"Opcja {i}" for i in range(30)]
    await page.set_content(
        "<body style='margin:0'><div style='height:400px'></div>"
        f"<select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    geo = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      window.__guidebot_selects.open(s);
      const list = window.__guidebot_selects.listFor(s);
      const sr = s.getBoundingClientRect();
      const lr = list.getBoundingClientRect();
      return {
        selectBottom: sr.bottom,
        listTop: lr.top,
        listBottom: lr.bottom,
        maxHeight: Number.parseFloat(getComputedStyle(list).maxHeight),
        overflowY: getComputedStyle(list).overflowY,
        scrollHeight: list.scrollHeight,
        clientHeight: list.clientHeight,
        innerHeight: window.innerHeight,
      };
    }"""
    )
    assert geo["listTop"] >= geo["selectBottom"] - 0.5, "the list flipped upward"
    assert geo["maxHeight"] >= 120
    assert geo["listBottom"] <= geo["innerHeight"] + 0.5, "the list spilled past the frame"
    assert geo["overflowY"] in ("auto", "scroll")
    assert geo["scrollHeight"] > geo["clientHeight"] + 1, "the list did not clamp"


async def test_list_never_flips_upward_and_keeps_a_floor_height(page: Page) -> None:
    """Right at the bottom edge there is no room; the floor wins, not a flip."""
    labels = [f"Opcja {i}" for i in range(30)]
    await page.set_content(
        "<body style='margin:0'><div style='height:690px'></div>"
        f"<select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    geo = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      window.__guidebot_selects.open(s);
      const list = window.__guidebot_selects.listFor(s);
      return {
        selectBottom: s.getBoundingClientRect().bottom,
        listTop: list.getBoundingClientRect().top,
        maxHeight: Number.parseFloat(getComputedStyle(list).maxHeight),
      };
    }"""
    )
    assert geo["listTop"] >= geo["selectBottom"] - 0.5, "the list flipped upward"
    assert abs(geo["maxHeight"] - 120) < 1.0, geo["maxHeight"]


async def test_close_hides_the_list_and_open_is_idempotent(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    states = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const list = api.listFor(s);
      const read = () => getComputedStyle(list).display;
      const out = [read()];
      api.open(s); out.push(read());
      api.open(s); out.push(read());
      api.close(s); out.push(read());
      api.close(s); out.push(read());
      return out;
    }"""
    )
    assert states == ["none", "block", "block", "none", "none"]


async def test_button_shows_the_selected_label_and_follows_external_changes(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    first = await page.evaluate(
        "() => window.__guidebot_selects.buttonFor(document.getElementById('s')).textContent.trim()"
    )
    assert first == "Mazowieckie"
    await page.evaluate("() => { document.getElementById('s').selectedIndex = 2; }")
    await page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )
    second = await page.evaluate(
        "() => window.__guidebot_selects.buttonFor(document.getElementById('s')).textContent.trim()"
    )
    assert second == "Śląskie"
