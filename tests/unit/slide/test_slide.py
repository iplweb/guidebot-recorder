"""Tests for the full-frame text-card overlay (``slide/`` package).

Mirrors ``tests/unit/overlay/test_overlay.py``: real Chromium via Playwright,
exercising the Python ``Slide`` controller (imported here as ``SlideCtl`` to
keep it visually distinct from ``guidebot_recorder.models.scenario.Slide``,
the unrelated step model) against the ``window.__guidebot_slide`` JS API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.slide.slide import Slide as SlideCtl


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        pg = await browser.new_page()
        try:
            yield pg
        finally:
            await browser.close()


async def test_show_mounts_single_card_and_leaves_page_dom_intact(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content('<main id="app">hello</main>')
    await ctl.install(page)

    await ctl.show(page, {"title": "Title", "subtitle": "Sub", "notes": "Notes"})

    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 1
    # the underlying document is untouched
    assert await page.eval_on_selector("#app", "el => el.textContent") == "hello"


async def test_show_escapes_text_via_textcontent_not_innerhtml(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)

    await ctl.show(page, {"title": "<b>T</b>", "subtitle": "S", "notes": None})

    title_text = await page.eval_on_selector(
        "[data-guidebot-slide-title]", "el => el.textContent"
    )
    assert title_text == "<b>T</b>"  # the literal string, not parsed markup

    card_html = await page.eval_on_selector("[data-guidebot-slide]", "el => el.innerHTML")
    assert "<b>" not in card_html
    assert "</b>" not in card_html


async def test_token_is_falsy_before_show_and_truthy_after(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)

    assert await page.evaluate("!!window.__guidebot_slide.token()") is False
    await ctl.show(page, {"title": "T", "subtitle": None, "notes": None})
    assert await page.evaluate("!!window.__guidebot_slide.token()") is True


async def test_token_is_monotone_across_repeated_shows(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)

    await ctl.show(page, {"title": "one", "subtitle": None, "notes": None})
    first = await page.evaluate("() => window.__guidebot_slide.token()")
    await ctl.show(page, {"title": "two", "subtitle": None, "notes": None})
    second = await page.evaluate("() => window.__guidebot_slide.token()")
    assert second > first


async def test_hide_removes_the_card_node(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)
    await ctl.show(page, {"title": "T", "subtitle": None, "notes": None})
    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 1

    await ctl.hide(page)

    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 0


async def test_ensure_rebuilds_a_missing_node_idempotently(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)
    card = {"title": "Rebuilt", "subtitle": None, "notes": None}
    await ctl.show(page, card)

    # A same-document rewrite (e.g. an SPA re-render) can wipe the DOM node while
    # keeping the JS context (window expandos, incl. the token, survive).
    await page.set_content("<main>SPA rerender</main>")
    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 0

    await ctl.ensure(page, card)

    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 1
    title_text = await page.eval_on_selector(
        "[data-guidebot-slide-title]", "el => el.textContent"
    )
    assert title_text == "Rebuilt"

    # ensure() is idempotent: calling it again with the node already present must
    # not create a second one.
    await ctl.ensure(page, card)
    assert await page.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 1


async def test_card_is_hit_testable_not_pointer_events_none(page: Page) -> None:
    """Unlike cursor/chrome, the card must stay hit-testable (no `pointer-events:none`).

    A stray click/hover during a card-up frame should fail Playwright's
    hit-target actionability check rather than silently click through to the
    hidden page underneath.
    """
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)
    await ctl.show(page, {"title": "T", "subtitle": None, "notes": None})

    pointer_events = await page.eval_on_selector(
        "[data-guidebot-slide]", "el => getComputedStyle(el).pointerEvents"
    )
    assert pointer_events != "none"


async def test_card_covers_full_viewport_at_max_z_index_with_opaque_background(
    page: Page,
) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)
    await ctl.show(page, {"title": "T", "subtitle": None, "notes": None})

    style = await page.eval_on_selector(
        "[data-guidebot-slide]",
        """el => {
            const s = getComputedStyle(el);
            return {
                position: s.position,
                zIndex: s.zIndex,
                background: s.backgroundColor,
            };
        }""",
    )
    assert style["position"] == "fixed"
    assert style["zIndex"] == "2147483647"
    assert style["background"] not in ("rgba(0, 0, 0, 0)", "transparent")

    box = await page.eval_on_selector(
        "[data-guidebot-slide]",
        "el => ({w: el.getBoundingClientRect().width, h: el.getBoundingClientRect().height})",
    )
    viewport = page.viewport_size
    assert viewport is not None
    assert box["w"] == pytest.approx(viewport["width"], abs=1)
    assert box["h"] == pytest.approx(viewport["height"], abs=1)


async def test_notes_supports_multiple_lines_verbatim(page: Page) -> None:
    ctl = SlideCtl()
    await page.set_content("<main>hello</main>")
    await ctl.install(page)
    notes = "line one\nline two\nline three"

    await ctl.show(page, {"title": None, "subtitle": None, "notes": notes})

    notes_text = await page.eval_on_selector(
        "[data-guidebot-slide-notes]", "el => el.textContent"
    )
    assert notes_text == notes


async def test_install_context_registers_api_for_future_top_level_documents(page: Page) -> None:
    ctl = SlideCtl()
    await ctl.install_context(page.context)
    await page.set_content("<button onclick=\"window.open('about:blank')\">Open</button>")

    async with page.expect_popup() as popup_info:
        await page.get_by_role("button", name="Open").click()
    popup = await popup_info.value

    assert await popup.evaluate("!!window.__guidebot_slide") is True
    # no card is auto-mounted; install only registers the API
    assert await popup.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 0


async def test_is_top_guard_prevents_mounting_inside_iframe(page: Page) -> None:
    """Under Spec A, context-level init scripts run in EVERY frame, including a
    sandboxed site iframe. Without the ``isTop`` guard at the top of ``slide.js``,
    the raw script would also mount a card inside the framed site's own document.
    """
    ctl = SlideCtl()
    await page.set_content('<iframe id="f" srcdoc="<html><body>framed</body></html>"></iframe>')
    handle = await page.wait_for_selector("#f")
    child_frame = await handle.content_frame()
    assert child_frame is not None
    await child_frame.wait_for_load_state()

    # Evaluate the raw script (as a context-level init script would) directly in
    # the framed (non-top) document.
    await child_frame.evaluate(ctl._script)
    assert await child_frame.evaluate("!!window.__guidebot_slide") is False
    assert (
        await child_frame.eval_on_selector_all("[data-guidebot-slide]", "els => els.length") == 0
    )

    # Sanity: the very same script mounts the API fine in the top document.
    await page.evaluate(ctl._script)
    assert await page.evaluate("!!window.__guidebot_slide") is True
