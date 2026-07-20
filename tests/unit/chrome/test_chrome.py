from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.config import ChromeConfig

HOST_SELECTOR = "[data-guidebot-chrome]"


class _RecordingPage:
    def __init__(self) -> None:
        self.evaluations: list[tuple[str, Any]] = []
        self.waits: list[float] = []

    async def evaluate(self, expression: str, arg: Any = None) -> bool | None:
        self.evaluations.append((expression, arg))
        if "const ready = !!api" in expression:
            return True
        return None

    async def wait_for_timeout(self, timeout: float) -> None:
        self.waits.append(timeout)


class _UnusedOverlay:
    pass


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


async def test_bare_popups_suppress_legacy_bar_on_popup_documents(page: Page) -> None:
    """With ``bare_popups`` the popup-site branch bails before mounting the bar.

    The floating-window compositor frames the popup in post, so no in-DOM chrome
    (bar host, ``__guidebot_chrome`` API, or reserved padding) must appear on the
    top-level popup document. The cursor overlay is a separate init script.
    """

    chrome = Chrome(ChromeConfig(enabled=True), bare_popups=True)
    await chrome.install_context(page.context)
    await page.set_content("<button onclick=\"window.open('about:blank')\">open</button>")

    async with page.expect_popup() as popup_info:
        await page.get_by_role("button", name="open").click()
    popup = await popup_info.value
    try:
        assert await popup.evaluate("() => window.__guidebot_chrome_config.barePopups") is True
        assert await popup.evaluate("() => window.__guidebot_chrome === undefined") is True
        assert await popup.locator(HOST_SELECTOR).count() == 0

        await popup.goto("data:text/html,<main>replacement document</main>")
        await popup.wait_for_load_state()
        assert await popup.evaluate("() => window.__guidebot_chrome === undefined") is True
        assert await popup.locator(HOST_SELECTOR).count() == 0
        padding = await popup.evaluate(
            "() => getComputedStyle(document.documentElement).paddingTop"
        )
        assert padding in ("0px", "")
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
    await page.evaluate("() => document.querySelector('[data-guidebot-shell-bar]').remove()")
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


async def test_type_url_sends_one_browser_side_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _RecordingPage()
    chrome = Chrome(ChromeConfig(enabled=True, pre_navigate_pause_ms=40))
    monkeypatch.setattr(
        "guidebot_recorder.chrome.chrome.typing_schedule",
        lambda *_args, **_kwargs: [10, 20, 30],
    )

    await chrome.type_url(
        page,  # type: ignore[arg-type]
        _UnusedOverlay(),
        "abc",
        seed="stable",
        choreograph=False,
    )

    # ensure_shell performs two invariant checks; arming cancellation plus the
    # whole animation are two more calls, independent of URL length.
    assert len(page.evaluations) == 4
    assert page.waits == []
    _, token = page.evaluations[-2]
    _, payload = page.evaluations[-1]
    assert payload == {
        "text": "abc",
        "delays": [10, 20, 30],
        "preNavigatePauseMs": 40,
        "token": token,
    }


async def test_type_url_browser_schedule_preserves_event_order_and_delays(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = Chrome(ChromeConfig(enabled=True, pre_navigate_pause_ms=40))
    await chrome.install_shell(page)
    monkeypatch.setattr(
        "guidebot_recorder.chrome.chrome.typing_schedule",
        lambda *_args, **_kwargs: [20, 30],
    )
    await page.evaluate(
        """() => {
            window.__guidebot_type_events = [];
            const api = window.__guidebot_shell;
            for (const method of ["focusPill", "clearUrl", "appendChar", "blurPill"]) {
                const original = api[method].bind(api);
                api[method] = (...args) => {
                    window.__guidebot_type_events.push({method, args, at: performance.now()});
                    return original(...args);
                };
            }
        }"""
    )

    await chrome.type_url(page, _UnusedOverlay(), "a😀", seed="stable", choreograph=False)

    events = await page.evaluate("window.__guidebot_type_events")
    assert [(event["method"], event["args"]) for event in events] == [
        ("focusPill", []),
        ("clearUrl", []),
        ("appendChar", ["a"]),
        ("appendChar", ["😀"]),
        ("blurPill", []),
    ]
    assert events[2]["at"] - events[1]["at"] >= 15
    assert events[3]["at"] - events[2]["at"] >= 25
    assert events[4]["at"] - events[3]["at"] >= 35


async def test_type_url_batched_schedule_stays_audible_per_character(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The batched browser-side schedule must not silence the sound feature: the
    # pill click plus one "key" per typed character are emitted by the sibling
    # asyncio timer that mirrors the schedule, with no per-character page.evaluate.
    class _SilentOverlay:
        async def move_to(self, page: Page, x: float, y: float) -> None:
            return None

        async def ripple(self, page: Page) -> None:
            return None

    chrome = Chrome(ChromeConfig(enabled=True, pre_navigate_pause_ms=10))
    await chrome.install_shell(page)
    monkeypatch.setattr(
        "guidebot_recorder.chrome.chrome.typing_schedule",
        lambda *_args, **_kwargs: [10, 10, 10],
    )

    sfx: list[str] = []
    await chrome.type_url(
        page,
        _SilentOverlay(),
        "abc",
        seed="stable",
        choreograph=True,
        on_sfx=sfx.append,
    )

    assert sfx == ["click", "key", "key", "key"]


async def test_type_url_cancellation_stops_browser_side_schedule(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = Chrome(ChromeConfig(enabled=True, pre_navigate_pause_ms=20))
    await chrome.install_shell(page)
    monkeypatch.setattr(
        "guidebot_recorder.chrome.chrome.typing_schedule",
        lambda *_args, **_kwargs: [10, 1000, 10],
    )

    task = asyncio.create_task(
        chrome.type_url(page, _UnusedOverlay(), "abc", seed="stable", choreograph=False)
    )
    await page.wait_for_function(
        """() => document.querySelector('[data-guidebot-shell-bar]')
            ?.shadowRoot.querySelector('[data-url]')?.textContent === 'a'"""
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Wait beyond the second character's schedule: browser-side work must have
    # stopped instead of outliving the cancelled Python task.
    await page.wait_for_timeout(1100)
    state = await page.evaluate(
        """() => {
            const bar = document.querySelector('[data-guidebot-shell-bar]');
            const pill = bar.shadowRoot.querySelector('[data-pill]');
            return {
                text: bar.shadowRoot.querySelector('[data-url]').textContent,
                focused: pill.hasAttribute('data-focused'),
                token: window.__guidebot_shell.__guidebotTypeToken ?? null,
            };
        }"""
    )
    assert state == {"text": "a", "focused": True, "token": None}


async def test_install_bar_mounts_on_a_page_under_bare_popups(page):
    # The context-wide script bails on `barePopups`; the per-page variant must
    # still be able to mount the bar on one window.
    chrome = Chrome(ChromeConfig(), bare_popups=True)
    await chrome.install_context(page.context)
    await page.goto("data:text/html,<p>karta</p>")

    assert await page.query_selector("[data-guidebot-chrome]") is None

    await chrome.install_bar(page)

    assert await page.query_selector("[data-guidebot-chrome]") is not None


async def test_install_bar_survives_a_navigation_inside_the_window(page):
    # The bar is mounted per page, so it must be re-registered as a per-page init
    # script: a `_blank` tab that navigates must not lose its address bar.
    chrome = Chrome(ChromeConfig(), bare_popups=True)
    await chrome.install_context(page.context)
    await page.goto("data:text/html,<p>karta</p>")
    await chrome.install_bar(page)

    await page.goto("data:text/html,<p>druga</p>")

    assert await page.query_selector("[data-guidebot-chrome]") is not None


async def test_install_context_alone_stays_bare(page):
    # The per-window override must not leak: a popup that never asked for a bar
    # keeps rendering exactly as it does today.
    chrome = Chrome(ChromeConfig(), bare_popups=True)
    await chrome.install_context(page.context)
    await page.goto("data:text/html,<p>plywajace</p>")

    assert await page.query_selector("[data-guidebot-chrome]") is None
