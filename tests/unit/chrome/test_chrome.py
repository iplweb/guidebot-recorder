from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.config import ChromeConfig

HOST_SELECTOR = "[data-guidebot-chrome]"


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def _snapshot(page: Page) -> dict:
    return await page.locator(HOST_SELECTOR).evaluate(
        """host => {
            const style = getComputedStyle(host);
            const shadow = host.shadowRoot;
            return {
                hasShadow: shadow !== null,
                pillCount: shadow?.querySelectorAll("[data-guidebot-url-pill]").length ?? 0,
                url: shadow?.querySelector("[data-guidebot-url-text]")?.textContent ?? null,
                lockCount: shadow?.querySelectorAll("[data-guidebot-lock]").length ?? 0,
                dotColors: Array.from(shadow?.querySelectorAll("[data-guidebot-dot]") ?? [])
                    .map((dot) => getComputedStyle(dot).backgroundColor),
                position: style.position,
                pointerEvents: style.pointerEvents,
                zIndex: style.zIndex,
                height: style.height,
                backgroundColor: style.backgroundColor,
                color: style.color,
                borderRadius: style.borderRadius,
                rootPadding: getComputedStyle(document.documentElement).paddingTop,
                rootPaddingPriority: document.documentElement.style
                    .getPropertyPriority("padding-top"),
            };
        }"""
    )


async def test_install_injects_bar_with_default_contract(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))

    await chrome.install(page)

    assert await page.evaluate("!!window.__guidebot_chrome") is True
    assert await page.evaluate(
        """() => ["ensure", "setUrl"].map(
            (name) => typeof window.__guidebot_chrome[name]
        )"""
    ) == ["function", "function"]
    assert await page.locator(HOST_SELECTOR).count() == 1
    snapshot = await _snapshot(page)
    assert snapshot == {
        "hasShadow": True,
        "pillCount": 1,
        "url": page.url,
        "lockCount": 0,
        "dotColors": ["rgb(255, 95, 87)", "rgb(254, 188, 46)", "rgb(40, 200, 64)"],
        "position": "fixed",
        "pointerEvents": "none",
        "zIndex": "2147483644",
        "height": "56px",
        "backgroundColor": "rgb(243, 244, 246)",
        "color": "rgb(55, 65, 81)",
        "borderRadius": "12px 12px 0px 0px",
        "rootPadding": "56px",
        "rootPaddingPriority": "important",
    }


async def test_cosmetic_config_drives_bar_appearance(page: Page) -> None:
    chrome = Chrome(
        ChromeConfig(
            enabled=True,
            height=72,
            barColor="#010203",
            textColor="#f0e0d0",
            radius=8,
            closeColor="#112233",
            minimizeColor="#445566",
            maximizeColor="#778899",
        )
    )

    await chrome.install(page)

    snapshot = await _snapshot(page)
    assert snapshot["height"] == "72px"
    assert snapshot["rootPadding"] == "72px"
    assert snapshot["backgroundColor"] == "rgb(1, 2, 3)"
    assert snapshot["color"] == "rgb(240, 224, 208)"
    assert snapshot["borderRadius"] == "8px 8px 0px 0px"
    assert snapshot["dotColors"] == [
        "rgb(17, 34, 51)",
        "rgb(68, 85, 102)",
        "rgb(119, 136, 153)",
    ]


async def test_set_url_waits_for_animation_and_updates_lock(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)
    target = "https://example.test/login"

    started = time.monotonic()
    await chrome.set_url(page, target)
    elapsed = time.monotonic() - started

    snapshot = await _snapshot(page)
    assert snapshot["url"] == target
    assert snapshot["lockCount"] == 1
    assert elapsed >= 0.1

    await chrome.set_url(page, "http://example.test/", animate=False)
    snapshot = await _snapshot(page)
    assert snapshot["url"] == "http://example.test/"
    assert snapshot["lockCount"] == 0


async def test_show_url_false_omits_pill_and_animation_delay(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True, showUrl=False))
    await chrome.install(page)
    await page.evaluate(
        """() => {
            const original = window.setTimeout;
            window.__guidebot_timeout_calls = 0;
            window.setTimeout = (...args) => {
                window.__guidebot_timeout_calls += 1;
                return original(...args);
            };
        }"""
    )

    await chrome.set_url(page, "https://example.test/a-very-long-path", animate=True)

    snapshot = await _snapshot(page)
    assert snapshot["pillCount"] == 0
    assert snapshot["url"] is None
    assert snapshot["lockCount"] == 0
    assert await page.evaluate("window.__guidebot_timeout_calls") == 0


async def test_install_registers_bar_for_future_documents(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)

    await page.goto("data:text/html,<main>next document</main>")

    assert await page.evaluate("!!window.__guidebot_chrome") is True
    assert await page.locator(HOST_SELECTOR).count() == 1
    snapshot = await _snapshot(page)
    assert snapshot["url"] == page.url
    assert snapshot["rootPadding"] == "56px"


async def test_install_context_injects_bar_into_popup_documents(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install_context(page.context)
    await page.set_content("<button onclick=\"window.open('about:blank')\">open</button>")

    async with page.expect_popup() as popup_info:
        await page.get_by_role("button", name="open").click()
    popup = await popup_info.value
    try:
        assert await popup.evaluate("!!window.__guidebot_chrome") is True
        await popup.goto("data:text/html,<main>replacement document</main>")
        await popup.wait_for_load_state()
        assert await popup.locator(HOST_SELECTOR).count() == 1
        snapshot = await _snapshot(popup)
        assert snapshot["url"] == popup.url
        assert snapshot["rootPadding"] == "56px"
    finally:
        await popup.close()


async def test_ensure_syncs_page_url_and_repairs_spa_wipe_without_padding_growth(
    page: Page,
) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)
    await chrome.set_url(page, "https://stale.example/", animate=False)

    await page.set_content("<main>SPA rerender</main>")
    await page.evaluate("delete window.__guidebot_chrome")
    assert await page.locator(HOST_SELECTOR).count() == 0

    await chrome.ensure(page)
    await chrome.ensure(page)

    assert await page.evaluate("!!window.__guidebot_chrome") is True
    assert await page.locator(HOST_SELECTOR).count() == 1
    snapshot = await _snapshot(page)
    assert snapshot["url"] == page.url
    assert snapshot["rootPadding"] == "56px"


async def test_ensure_does_not_grow_padding_when_spa_removes_marker(page: Page) -> None:
    await page.set_content("<style>html { padding-top: 10px; }</style><main>page</main>")
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)
    assert (await _snapshot(page))["rootPadding"] == "66px"

    await page.evaluate(
        "document.documentElement.removeAttribute('data-guidebot-chrome-base-padding')"
    )
    await chrome.ensure(page)
    await chrome.ensure(page)

    assert (await _snapshot(page))["rootPadding"] == "66px"


# --- hide()/show() persistent flag (role-dispatching) -----------------------


async def test_legacy_hidden_flag_survives_ensure(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install(page)

    await chrome.hide(page)
    await chrome.ensure(page)

    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-chrome]')).display"
    )
    assert disp == "none"

    await chrome.show(page)
    disp2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-chrome]')).display"
    )
    assert disp2 != "none"


async def test_shell_hidden_flag_survives_ensure_shell_without_hiding_iframe(
    page: Page,
) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install_shell(page)

    await chrome.hide(page)
    await chrome.ensure_shell(page)  # re-assert, as the render loop does every step

    bar_display = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-shell-bar]')).display"
    )
    assert bar_display == "none"
    # the bar node must stay IN THE DOM (display:none, not removed) so
    # ensure_shell's readiness check keeps passing.
    assert await page.locator("[data-guidebot-shell-bar]").count() == 1
    # the site iframe is untouched by the flag — still present and visible.
    assert await page.locator("iframe#guidebot-site").count() == 1
    iframe_display = await page.evaluate(
        "getComputedStyle(document.querySelector('iframe#guidebot-site')).display"
    )
    assert iframe_display != "none"

    await chrome.show(page)
    bar_display2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-shell-bar]')).display"
    )
    assert bar_display2 != "none"


async def test_shell_hidden_flag_survives_reinjection(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await chrome.install_shell(page)

    await chrome.hide(page)

    # Force the reinjection path: remove the bar node so ensure_shell sees
    # _SHELL_IS_READY == false and re-evaluates shell.js. window.__guidebot_shell
    # still exists, so the reentry guard must preserve the closure (and `hidden`)
    # rather than re-running the whole IIFE and resetting the flag to false.
    await page.evaluate(
        "() => document.querySelector('[data-guidebot-shell-bar]').remove()"
    )
    await chrome.ensure_shell(page)

    assert await page.locator("[data-guidebot-shell-bar]").count() == 1
    bar_display = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-shell-bar]')).display"
    )
    assert bar_display == "none"


async def test_hide_show_are_no_op_when_neither_api_is_present(page: Page) -> None:
    chrome = Chrome(ChromeConfig(enabled=True))
    await page.set_content("<main>plain page, no chrome installed</main>")

    # must not raise even though neither __guidebot_chrome nor __guidebot_shell exist
    await chrome.hide(page)
    await chrome.show(page)
