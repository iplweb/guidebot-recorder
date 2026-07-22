"""Tests for the Python ``Selects`` controller (config prelude + installation)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import BrowserContext, async_playwright

from guidebot_recorder.models.config import SelectsConfig
from guidebot_recorder.selects import Selects
from guidebot_recorder.selects.selects import (
    DEFERRAL_FACTOR,
    READY_TIMEOUT,
    SelectsNotReadyError,
)
from guidebot_recorder.selects.visibility import (
    SELECT_SHAPE_GLOBAL,
    SELECT_SHAPE_JS,
    select_shape,
)


def _prelude(script: str) -> dict:
    match = re.match(r"window\.__guidebot_selects_config = (\{.*\});\n", script)
    assert match is not None, script[:200]
    return json.loads(match.group(1))


def test_prelude_carries_the_config_as_camel_case_json() -> None:
    selects = Selects(SelectsConfig(mode="native", settle_ms=250, max_visible_options=3))
    assert _prelude(selects.script) == {
        "mode": "native",
        "settleMs": 250,
        "maxVisibleOptions": 3,
    }


def test_defaults_are_used_when_no_config_is_given() -> None:
    assert _prelude(Selects().script) == _prelude(Selects(SelectsConfig()).script)
    assert _prelude(Selects().script)["mode"] == "shim"


def test_open_hold_ms_stays_python_side() -> None:
    """It paces the recorder's second beat; the widget has no use for it."""
    assert "openHoldMs" not in _prelude(Selects(SelectsConfig(open_hold_ms=900)).script)


def test_script_is_the_prelude_followed_by_the_widget_body() -> None:
    script = Selects().script
    assert script.startswith("window.__guidebot_selects_config = ")
    assert "__guidebot_selects" in script
    assert "data-guidebot-select-button" in script
    # ...and the shared predicate, which the widget body does not restate
    assert SELECT_SHAPE_GLOBAL in script


# --- one owner for "is this select already enhanced?" -----------------------


def test_every_consumer_reads_the_same_already_enhanced_predicate() -> None:
    """Four independent definitions of "visible / already enhanced" is how two
    regressions reached this branch, so the rule has exactly one home and every
    consumer is pinned to it here.

    ``selects.js`` reads it off the global the controller installs; the recorder
    embeds the source in its own classification pass; the resolver calls the
    Python accessor. None of the three may grow a copy of the rule — a copy is
    invisible until the day the two answers differ.
    """

    from guidebot_recorder.recorder.select import _js as recorder_js
    from guidebot_recorder.resolver import widget as widget_module

    widget_body = files("guidebot_recorder.selects").joinpath("selects.js").read_text("utf-8")

    assert SELECT_SHAPE_JS in recorder_js._SHIM_STATE_JS
    assert widget_module.select_shape is select_shape
    assert SELECT_SHAPE_GLOBAL in widget_body

    # The marker-class list and the 8x8 floor are the rule itself: if either
    # appears anywhere but in `visibility.js`, the copy is already back.
    for source in (widget_body, recorder_js._SHIM_STATE_JS.replace(SELECT_SHAPE_JS, "")):
        assert "select2-hidden-accessible" not in source
        assert "rect.width < 8" not in source


def test_ready_timeout_is_derived_from_settle_ms() -> None:
    """M6: a fixed 15 s ceiling is shorter than the widget's own 3 x settle cap.

    With ``settle_ms >= 5000`` the page is still allowed to be classifying when a
    hard-coded timeout would already have raised, so ordinary page churn produced
    a spurious ``SelectsNotReadyError``.
    """
    assert Selects(SelectsConfig(settle_ms=1000)).ready_timeout == READY_TIMEOUT
    for settle_ms in (5000, 8000, 30000):
        derived = Selects(SelectsConfig(settle_ms=settle_ms)).ready_timeout
        assert derived > settle_ms / 1000 * DEFERRAL_FACTOR, settle_ms
        assert derived >= READY_TIMEOUT, settle_ms


@pytest.fixture
async def context() -> AsyncIterator[BrowserContext]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        try:
            yield ctx
        finally:
            await browser.close()


async def test_install_context_shims_every_new_document(context: BrowserContext) -> None:
    selects = Selects(SelectsConfig(settle_ms=20))
    await selects.install_context(context)
    page = await context.new_page()
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:200px'>"
        "<option>a</option><option>b</option></select></body>"
    )
    await selects.wait_ready(page)
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )


async def test_wait_ready_covers_a_select_the_page_added_after_the_first_pass(
    context: BrowserContext,
) -> None:
    """The barrier is per-step, so it must answer for the DOM *this* step sees.

    Compile and render take it before every step with a target, precisely so the
    step drives shimmed DOM. Awaiting the widget's one-shot `ready` made it
    answer for page load instead: a select appended by the previous step was
    still unclassified when this returned, and the `select:` step that followed
    failed with "the shim did not cover it".
    """
    selects = Selects(SelectsConfig(settle_ms=400))
    await selects.install_context(context)
    page = await context.new_page()
    # A real navigation, not `set_content`: the latter replaces
    # `document.documentElement`, which strands the widget's observer on the
    # detached root and would make this assert about that instead.
    await page.goto("data:text/html,<body style='margin:0'></body>")
    await selects.wait_ready(page)

    await page.evaluate(
        "() => { const s = document.createElement('select');"
        " s.id = 'late'; s.style.width = '200px';"
        " s.innerHTML = '<option>a</option><option>b</option>';"
        " document.body.appendChild(s); }"
    )
    await selects.wait_ready(page)

    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('late'))"
    )


async def test_wait_ready_falls_back_to_ready_on_a_partial_api(context: BrowserContext) -> None:
    """An API object without `settled` is still a barrier, not a crash.

    The two sides are injected together in production, so this only ever covers
    a page carrying a stub or an older copy — but the barrier reaching for a
    method that is not there would turn that into a `TypeError` in the page,
    surfacing as neither of this method's documented failures.
    """
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate("() => { window.__guidebot_selects = {ready: Promise.resolve()}; }")

    await Selects().wait_ready(page, timeout=2.0)


async def test_install_context_reaches_nested_iframes(context: BrowserContext) -> None:
    """A select in a nested iframe needs shimming just as much (spec §1)."""
    selects = Selects(SelectsConfig(settle_ms=20))
    await selects.install_context(context)
    page = await context.new_page()
    await page.set_content("<body style='margin:0'><iframe id='f' srcdoc=\"\"></iframe></body>")
    await page.evaluate(
        """() => new Promise((resolve) => {
      const frame = document.getElementById('f');
      frame.addEventListener('load', resolve, {once: true});
      frame.srcdoc = "<body style='margin:0'><select id='s' style='width:200px'>"
        + "<option>a</option><option>b</option></select></body>";
    })"""
    )
    child = page.frames[1]
    await selects.wait_ready(child)
    assert await child.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )


async def test_wait_ready_fails_loudly_when_the_widget_is_absent(
    context: BrowserContext,
) -> None:
    """And it fails as its own documented exception, in the project's language.

    The page-side throw used to travel out verbatim: an English
    ``playwright.Error``, contradicting this method's own ``Raises:`` clause and
    uncatchable for a caller who catches ``SelectsNotReadyError``. Asserting on
    the English marker text was asserting the bug.
    """

    page = await context.new_page()
    await page.set_content("<div></div>")
    with pytest.raises(SelectsNotReadyError, match="nie został wstrzyknięty") as raised:
        await Selects().wait_ready(page)

    message = str(raised.value)
    assert "about:blank" in message, message  # names the frame that had no widget
    assert "guidebot selects" not in message  # the page-side marker stays internal


def test_selects_not_ready_error_is_importable_from_the_package() -> None:
    """A caller must be able to catch it without reaching into the submodule."""

    import guidebot_recorder.selects as package

    assert "SelectsNotReadyError" in package.__all__
    assert package.SelectsNotReadyError is SelectsNotReadyError


async def test_wait_ready_uses_the_derived_deadline_by_default(context: BrowserContext) -> None:
    """M6: the page-side race must be armed with the derived timeout, not 15 s."""
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate(
        """() => {
      window.__delays = [];
      const original = window.setTimeout;
      window.setTimeout = function (fn, ms, ...rest) {
        window.__delays.push(ms);
        return original.call(window, fn, ms, ...rest);
      };
      window.__guidebot_selects = {ready: Promise.resolve()};
    }"""
    )
    selects = Selects(SelectsConfig(settle_ms=9000))
    await selects.wait_ready(page)
    assert await page.evaluate("() => window.__delays") == [int(selects.ready_timeout * 1000)]


async def test_wait_ready_times_out_instead_of_hanging_forever(context: BrowserContext) -> None:
    """C1: a `ready` that never settles must fail loudly, not hang the render."""
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate(
        "() => { window.__guidebot_selects = {ready: new Promise(() => {})}; }",
    )
    with pytest.raises(SelectsNotReadyError, match="nie zgłosił gotowości") as raised:
        await Selects().wait_ready(page, timeout=0.4)
    # M7: the message has to name the frame and a way out, not just the deadline.
    message = str(raised.value)
    assert "about:blank" in message, message
    assert "settleMs" in message, message
    assert "native" in message, message


async def test_wait_ready_timeout_leaves_a_working_page(context: BrowserContext) -> None:
    """The `asyncio.wait_for` cancellation path must not wedge the connection.

    M9: the page-side race is disabled here on purpose — with it armed the
    evaluate rejects on its own and the outer cancellation this test is named
    for never runs at all.
    """
    page = await context.new_page()
    await page.set_content("<div></div>")
    await page.evaluate(
        """() => {
      // Nothing schedules in this page any more, so the widget's own timeout
      // promise can never reject: only Python's outer wait can fire.
      window.setTimeout = () => 0;
      window.__guidebot_selects = {ready: new Promise(() => {})};
    }"""
    )
    started = time.monotonic()
    with pytest.raises(SelectsNotReadyError):
        await Selects().wait_ready(page, timeout=0.4)
    elapsed = time.monotonic() - started
    # The outer wait is `timeout + 1.0`; a page-side rejection would have landed
    # at 0.4 s, so this is proof of which of the two guards actually fired.
    assert elapsed > 1.0, f"the page-side race fired after {elapsed:.2f} s, not asyncio.wait_for"
    assert await page.evaluate("() => 1 + 1") == 2
