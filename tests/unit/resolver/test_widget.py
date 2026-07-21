from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.resolver.widget import associated_control, user_visible_control


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 640, "height": 480})
        try:
            yield page
        finally:
            await browser.close()


# --- associated_control: the four-step heuristic, in isolation -------------


async def test_associated_control_step1_resolves_aria_controls(page):
    await page.set_content(
        """
        <select id="sel" aria-controls="widget"><option>a</option></select>
        <div id="widget" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_associated_control_step1_resolves_aria_owns(page):
    await page.set_content(
        """
        <select id="sel" aria-owns="widget"><option>a</option></select>
        <div id="widget" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_associated_control_step2_resolves_backreferencing_labelledby(page):
    await page.set_content(
        """
        <select id="sel"><option>a</option></select>
        <div id="widget" aria-labelledby="sel" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_associated_control_step2_resolves_backreferencing_describedby(page):
    await page.set_content(
        """
        <select id="sel"><option>a</option></select>
        <div id="widget" aria-describedby="sel" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_associated_control_step3_resolves_nearest_following_sibling_with_a_box(page):
    await page.set_content(
        """
        <select id="sel"><option>a</option></select>
        <div id="empty" style="width:0;height:0;"></div>
        <div id="widget" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_associated_control_returns_none_when_nothing_matches(page):
    await page.set_content('<select id="sel"><option>a</option></select>')

    handle = await associated_control(page.locator("#sel"))

    assert handle is None


# --- associated_control: priority order when more than one step could match -


async def test_associated_control_prefers_aria_controls_over_labelledby_backreference(page):
    await page.set_content(
        """
        <select id="sel" aria-controls="via-controls"><option>a</option></select>
        <div id="via-labelledby" aria-labelledby="sel" style="width:100px;height:20px;"></div>
        <div id="via-controls" style="width:100px;height:20px;"></div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "via-controls"


async def test_associated_control_prefers_labelledby_backreference_over_following_sibling(page):
    await page.set_content(
        """
        <select id="sel"><option>a</option></select>
        <div id="via-sibling" style="width:100px;height:20px;"></div>
        <div id="via-labelledby" aria-labelledby="sel" style="width:100px;height:20px;"></div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "via-labelledby"


async def test_associated_control_prefers_aria_controls_over_following_sibling(page):
    await page.set_content(
        """
        <select id="sel" aria-controls="via-controls"><option>a</option></select>
        <div id="via-sibling" style="width:100px;height:20px;"></div>
        <div id="via-controls" style="width:100px;height:20px;"></div>
        """
    )
    handle = await associated_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "via-controls"


# --- user_visible_control: precedence select > shim button > associated ----


async def test_user_visible_control_prefers_the_select_when_it_is_visible(page):
    await page.set_content(
        """
        <select id="sel" data-guidebot-shimmed="uid-1"><option>a</option></select>
        <div data-guidebot-select-button data-guidebot-for="uid-1"
             style="width:100px;height:20px;"></div>
        """
    )
    handle = await user_visible_control(page.locator("#sel"))

    assert await handle.evaluate("element => element.tagName.toLowerCase()") == "select"


async def test_user_visible_control_falls_back_to_the_shim_button(page):
    await page.set_content(
        """
        <select id="sel" data-guidebot-shimmed="uid-1" style="display:none;">
          <option>a</option>
        </select>
        <div data-guidebot-select-button data-guidebot-for="uid-1"
             style="width:100px;height:20px;"></div>
        """
    )
    handle = await user_visible_control(page.locator("#sel"))

    assert await handle.get_attribute("data-guidebot-select-button") is not None


async def test_user_visible_control_ignores_a_different_selects_visible_shim_button(page):
    """Step 3's sibling scan must skip another select's shim button even when
    that button is genuinely on-screen -- its normal, non-``display:none``
    state, since a shim button is pinned to the rectangle of a real element.
    Shim overlays are appended to ``<body>`` right alongside the selects they
    replace, so a foreign button landing as this select's nearest following
    sibling is a realistic arrangement, not a contrived one. It must never be
    mistaken for this select's own widget.
    """
    await page.set_content(
        """
        <select id="sel" data-guidebot-shimmed="uid-1" style="display:none;">
          <option>a</option>
        </select>
        <div data-guidebot-select-button data-guidebot-for="uid-OTHER"
             style="width:50px;height:10px;"></div>
        <div id="widget" style="width:100px;height:20px;">fallback widget</div>
        """
    )
    handle = await user_visible_control(page.locator("#sel"))

    # No matching shim button for this select's own uid, so the uid lookup
    # falls through to the associated-control heuristic. Step 3 must skip the
    # foreign shim button -- which has a genuine, non-empty box here, unlike a
    # display:none button that the ordinary bounding-box check would already
    # exclude -- and continue on to the real fallback widget.
    assert await handle.get_attribute("id") == "widget"


async def test_user_visible_control_falls_back_to_associated_control(page):
    await page.set_content(
        """
        <select id="sel" style="display:none;" aria-controls="widget">
          <option>a</option>
        </select>
        <div id="widget" style="width:100px;height:20px;">widget</div>
        """
    )
    handle = await user_visible_control(page.locator("#sel"))

    assert await handle.get_attribute("id") == "widget"


async def test_user_visible_control_returns_none_when_nothing_qualifies(page):
    await page.set_content('<select id="sel" style="display:none;"><option>a</option></select>')

    handle = await user_visible_control(page.locator("#sel"))

    assert handle is None


async def test_user_visible_control_returns_none_when_shim_button_itself_is_hidden(page):
    await page.set_content(
        """
        <select id="sel" data-guidebot-shimmed="uid-1" style="display:none;">
          <option>a</option>
        </select>
        <div data-guidebot-select-button data-guidebot-for="uid-1"
             style="display:none;"></div>
        """
    )
    handle = await user_visible_control(page.locator("#sel"))

    assert handle is None
