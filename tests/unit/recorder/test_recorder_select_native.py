"""The ``native`` escape hatch: travel the cursor to the control, set at once.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding. This file covers the
per-step ``native: true`` override on its own; the *disabled-option* fast-fails
that this hatch shares with the compile path live in
``test_recorder_select_verify.py`` and ``test_recorder_select_reason.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder

from ._recorder_select_helpers import _raw_page, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


_PLAIN_SELECT = (
    "<select aria-label='Raport'>"
    "<option>lista</option><option>tabela</option><option>BibTeX</option>"
    "</select>"
)


_KEYDOWN_SPY = """() => {
  window.__gbKeys = [];
  document.addEventListener("keydown", (event) => { window.__gbKeys.push(event.key); }, true);
}"""


async def _keys(page: Page) -> list[str]:
    return await page.evaluate("() => window.__gbKeys")


async def test_native_with_overlay_sets_the_value_without_stepping(page):
    """`mode: native` travels the cursor to the control and sets the value at once.

    The old escape hatch stepped the value with `ArrowDown`/`ArrowUp` presses.
    That is *platform-dependent*, which is why it is gone. Measured with this
    repo's pinned Playwright on **macOS**, headless and headed: pressing
    `ArrowDown` twice on a focused native `<select>` leaves `selectedIndex` at 0
    and fires no `change` — macOS binds those keys on a closed menulist to
    opening the OS popup instead. On Linux and Windows Chromium (this repo's CI
    is `ubuntu-latest`) the same presses do step the value and do fire `change`.

    An animation that exists on one platform and not another renders two
    different films from one scenario, so the hatch keeps only what is portable:
    the cursor travels, the ripple plays, the value is set at once. No arrow
    presses, no `key` SFX, on any platform.
    """
    overlay = Overlay()
    await page.set_content(_PLAIN_SELECT)
    await overlay.install(page)
    await page.evaluate(_KEYDOWN_SPY)
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True)

    assert await page.locator("select").input_value() == "BibTeX"
    assert overlay.pos != (0.0, 0.0)  # cursor travelled to the control
    assert events == ["click"]  # ripple only — no per-step "key" SFX
    assert await _keys(page) == []  # no arrow keys were ever pressed


async def test_native_under_a_global_shim_drives_a_genuinely_native_control(page):
    """The per-step escape hatch has to work where it is needed: under the shim.

    It unshims the select first (see `_pin_native`) so the cursor lands on the
    real control rather than a widget that is about to disappear, then sets the
    value directly — no arrow keys are involved at any point.
    """
    overlay = await _raw_page(page)
    await page.evaluate(_KEYDOWN_SPY)
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append, open_hold_ms=10)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True)

    assert await page.locator("select").input_value() == "BibTeX"
    assert events == ["click"]
    assert await _keys(page) == []  # no arrow keys were ever pressed
    state = await page.evaluate(
        """() => ({
      shimmed: window.__guidebot_selects.isShimmed(document.querySelector('select')),
      overlays: document.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]').length,
    })"""
    )
    assert state == {"shimmed": False, "overlays": 0}


async def test_native_without_overlay_takes_the_direct_path(page):
    await page.set_content(_PLAIN_SELECT)
    events: list[str] = []
    rec = Recorder(page, overlay=None, on_sfx=events.append)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True)

    assert await page.locator("select").input_value() == "BibTeX"
    assert events == ["click"]  # no arrow stepping without an overlay
