"""``mode: native`` and ``pinNative``: handing the browser's own control back.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map. Two ways in: the global ``mode: native`` config, which installs no shim at
all yet still has to offer the whole API, and the per-step ``pinNative`` escape
hatch, which has to *undo* a shim that already exists and make the undoing
stick.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import NESTED, SELECTS_JS, _inject, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


# --- the per-step `mode: native` escape hatch ------------------------------
#
# A global `shim` plus one stubborn widget is the case the per-step override
# exists for. The select is already shimmed by then, so opting out has to *undo*
# the shim — and stay undone, or the next classification pass shims it right back
# under the recorder's feet.


async def test_pin_native_unshims_the_select_and_leaves_no_overlay(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )

    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      window.__guidebot_selects.pinNative(s);
      return {
        overlays: document.querySelectorAll(
          '[data-guidebot-select-button],[data-guidebot-select-list]').length,
        isShimmed: window.__guidebot_selects.isShimmed(s),
        shimMarker: s.hasAttribute('data-guidebot-shimmed'),
        nativeMarker: s.hasAttribute('data-guidebot-native'),
      };
    }"""
    )

    assert state == {
        "overlays": 0,
        "isShimmed": False,
        "shimMarker": False,
        "nativeMarker": True,
    }


async def test_a_pinned_native_select_is_not_reshimmed_by_the_next_pass(page: Page) -> None:
    """The marker is durable: `classify()` honours it for the rest of the run."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.pinNative(document.getElementById('s'))")

    # A page mutation the observer reacts to, plus a late select that proves the
    # pass really ran rather than merely never having been scheduled.
    await page.evaluate(
        "() => { document.getElementById('main').insertAdjacentHTML('beforeend',"
        ' \'<select id="late" style="width:200px"><option>a</option><option>b</option></select>\');'
        " }"
    )
    await page.wait_for_function(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('late'))",
        timeout=3000,
    )

    state = await page.evaluate(
        """() => ({
      pinned: window.__guidebot_selects.isShimmed(document.getElementById('s')),
      overlays: document.querySelectorAll('[data-guidebot-select-button]').length,
    })"""
    )
    assert state == {"pinned": False, "overlays": 1}  # only the late select's


async def test_a_pinned_native_select_no_longer_swallows_keys_or_mousedown(page: Page) -> None:
    """What the escape hatch is *for*: the browser's own control, back in charge.

    While shimmed, the widget's capture-phase handlers `preventDefault()` both —
    which would otherwise stop the real, unshimmed control from ever seeing a
    keydown or mousedown meant for it once `mode: native` hands it back.
    """
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.pinNative(document.getElementById('s'))")

    prevented = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const key = new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true});
      s.dispatchEvent(key);
      const down = new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0});
      s.dispatchEvent(down);
      return {key: key.defaultPrevented, mousedown: down.defaultPrevented};
    }"""
    )
    assert prevented == {"key": False, "mousedown": False}


async def test_pin_native_is_idempotent_and_harmless_on_an_unshimmed_select(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate(
        """() => {
      const api = window.__guidebot_selects;
      const s = document.getElementById('s');
      api.pinNative(s);
      api.pinNative(s);
      api.pinNative(null);
      api.pinNative(document.getElementById('main'));
    }"""
    )
    assert (
        await page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-button]').length"
        )
        == 0
    )


async def test_native_mode_installs_no_shim_but_still_resolves_ready(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page, {"mode": "native"})
    state = await page.evaluate(
        """() => ({
      overlays: document.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]').length,
      isShimmed: window.__guidebot_selects.isShimmed(document.getElementById('s')),
      marker: document.getElementById('s').hasAttribute('data-guidebot-shimmed'),
    })"""
    )
    assert state == {"overlays": 0, "isShimmed": False, "marker": False}


async def test_native_mode_offers_the_whole_api_as_no_ops(page: Page) -> None:
    """compile/render call this surface unconditionally — no `mode` branch of their own."""
    await page.set_content(NESTED)
    await _inject(page, {"mode": "native"})
    surface = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const missing = [
        'ready', 'isShimmed', 'buttonFor', 'listFor', 'isOpen', 'open', 'close',
        'optionIndexFor', 'scrollOptionIntoView', 'refresh', 'pinNative', 'settled',
      ].filter((name) => api[name] === undefined);
      api.open(s);
      api.close(s);
      api.refresh();
      api.scrollOptionIntoView(s, 1);
      api.pinNative(s);
      return {
        missing: missing,
        readyIsShared: api.ready === window.__guidebot_selects_ready,
        isShimmed: api.isShimmed(s),
        buttonFor: api.buttonFor(s),
        listFor: api.listFor(s),
        isOpen: api.isOpen(s),
        optionIndexFor: api.optionIndexFor(s, 'Mazowieckie'),
        selectedIndex: s.selectedIndex,
      };
    }"""
    )
    assert surface == {
        "missing": [],
        "readyIsShared": True,
        "isShimmed": False,
        "buttonFor": None,
        "listFor": None,
        "isOpen": False,
        "optionIndexFor": -1,
        "selectedIndex": 0,
    }


async def test_native_mode_offers_settled_as_a_resolved_barrier(page: Page) -> None:
    """`mode: native` classifies nothing, so no pass can ever be owed.

    The shimmed half of this contract — what `settled()` waits for, and that it
    resolves at all under a mutation storm or a throwing pass — is asserted in
    `test_selects_js_readiness.py`. This is the degenerate case that keeps
    compile and render from having to branch on `mode` before taking a barrier.
    """
    await page.set_content(NESTED)
    await page.evaluate('window.__guidebot_selects_config = {"mode": "native"};')
    await page.evaluate(SELECTS_JS)

    outcome = await page.evaluate(
        """() => Promise.race([
      window.__guidebot_selects.settled().then(() => 'settled'),
      new Promise((r) => window.setTimeout(() => r('never settled'), 1000)),
    ])"""
    )
    assert outcome == "settled"
