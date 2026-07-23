"""The readiness barrier is bounded here too — and covers mutations, not just the first pass.

``Recorder.select`` awaits ``Selects.wait_ready`` before it drives anything, so a
page whose classification promise never settles gets a diagnosis rather than an
unbounded hang. The same barrier has to cover the pass a *mutation* owes: a
``<select>`` the page appends mid-run is unshimmed for a whole settle window
while every earlier barrier already reported "ready".

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder

# ``READY_WAIT_MS`` is patched below purely for speed (300 ms instead of 15 s) by
# the "never settles" test, which then asserts on the raised type. It is patched
# on ``select.probe`` — the module whose globals the waiting code reads at call
# time — because naming the wrong module (or importing the constant by name)
# would rebind something nobody consults: the test would still pass and simply
# take fifteen seconds longer. ``test_recorder_seams.py`` makes that a failure.
from guidebot_recorder.recorder.select import probe as select_probe
from guidebot_recorder.selects import SelectsNotReadyError

from ._recorder_select_helpers import (
    _MOUSEDOWN_SPY,
    _RAW_SELECT,
    _hits,
    _install_selects,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


# --- the readiness barrier is bounded here too ------------------------------


async def test_select_gives_up_on_a_page_whose_readiness_never_settles(page, monkeypatch):
    """`Recorder.select` must not be the one call that can wait forever.

    Compile and render both take `Selects.wait_ready` — a bounded barrier —
    before they get here, so today this await always finds a settled promise.
    That invariant is enforced nowhere near this method, though, and an
    unbounded await on a page-controlled promise is exactly the hang
    `wait_ready` exists to prevent. A direct caller on a wedged page must get a
    diagnosis, not silence.
    """

    monkeypatch.setattr(select_probe, "READY_WAIT_MS", 300)
    await page.set_content(_RAW_SELECT)
    # A widget that never finishes its first classification pass.
    await page.evaluate(
        """() => {
      window.__guidebot_selects = {
        ready: new Promise(() => {}),
        isShimmed: () => false,
      };
    }"""
    )
    rec = Recorder(page, overlay=None)

    with pytest.raises(SelectsNotReadyError):
        # `wait_for` so a regression fails the suite instead of hanging it.
        await asyncio.wait_for(
            rec.select(RoleTarget(role="combobox", name="Raport"), "tabela"),
            timeout=10,
        )

    assert await page.locator("select").input_value() == "lista"  # value untouched


async def test_select_still_treats_a_missing_widget_as_nothing_to_wait_for(page):
    """No shim in this context is not a wedged page — it is a bare document.

    The recorder is handed a page, not the controller that installed the
    widget, so "the API is not here" has to degrade to "there is nothing to
    wait for". Only a promise that never settles is a failure.
    """

    await page.set_content(_RAW_SELECT)
    rec = Recorder(page, overlay=None)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert await page.locator("select").input_value() == "tabela"


# --- a select the page adds mid-run ----------------------------------------


#: A form that grows a criteria row on demand, the shape every "add another
#: filter" page has. The new row's ``<select>`` exists only after the click, so
#: it is classified by a *later* pass than the one ``ready`` reports.
_GROWING_FORM = (
    "<body style='margin:0'>"
    "<button id='add' style='width:200px;height:30px'>Dodaj pole</button>"
    "<div id='rows'></div>"
    "<script>"
    "document.getElementById('add').addEventListener('click', () => {"
    "  const s = document.createElement('select');"
    "  s.setAttribute('aria-label', 'Pole');"
    "  s.style.width = '220px';"
    "  s.innerHTML = '<option>Tytuł pracy</option><option>Zakres lat</option>';"
    "  document.getElementById('rows').appendChild(s);"
    "});"
    "</script></body>"
)


async def test_a_select_added_mid_run_is_driven_through_the_shim(page):
    """The readiness barrier must cover the pass a *mutation* owes, not just the first.

    `ready` resolves once, at the first classification pass, and never re-arms.
    A select the page appends mid-run is therefore unshimmed for a whole settle
    window while every barrier in compile and render reports "ready" — so a
    `select:` step landing inside that window found a bare `<select>`, could not
    unfurl a DOM list for it, and failed with "nakładka jej nie objęła".

    Recorded against the real symptom: a criteria row added by an "add field"
    button, then chosen from in the very next step.
    """

    overlay = Overlay()
    await page.set_content(_GROWING_FORM)
    await overlay.install(page)
    # Long enough that the pass the click owes is still pending when the next
    # step starts — the production default is 1000 ms.
    await _install_selects(page, settleMs=400)
    await page.evaluate(_MOUSEDOWN_SPY)
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.click(RoleTarget(role="button", name="Dodaj pole"))
    await rec.select(RoleTarget(role="combobox", name="Pole"), "Zakres lat")

    assert await page.locator("#rows select").input_value() == "Zakres lat"
    # ...and it was chosen *on camera*: beat 2 clicked our option row, which
    # only exists when the shim covered this select.
    assert await _hits(page) == ["button", "select", "option:1"]
