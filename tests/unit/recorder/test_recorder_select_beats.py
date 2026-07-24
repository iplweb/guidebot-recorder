"""Render-time choreography for ``select:`` steps (spec §4).

The two beats — cursor opens the list, cursor clicks the option — are what makes
a dropdown visible on camera at all, so they are asserted through what a viewer
would actually see: where the synthetic cursor travelled, which element received
the pointer, and whether the wanted row was scrolled into the list's viewport
before the cursor set off towards it.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder

from ._recorder_select_helpers import (
    _enhanced,
    _hits,
    _install_selects,
    _raw_page,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


# --- the two beats ---------------------------------------------------------


async def test_two_beats_fire_in_order_with_two_click_sounds(page):
    overlay = await _raw_page(page)
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append, open_hold_ms=10)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX")

    assert await page.locator("select").input_value() == "BibTeX"
    # beat 1 opened the list on the select, beat 2 clicked option index 2
    assert await _hits(page) == ["select", "option:2"]
    assert events == ["click", "click"]


async def test_shimmed_select_is_clicked_on_the_select_not_the_shim_button(page):
    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    hits = await _hits(page)
    assert hits[0] == "select"
    assert "button" not in hits


async def test_list_is_scrolled_to_the_option_before_the_cursor_glides(page):
    overlay = Overlay()
    options = "".join(f"<option>Opcja {i}</option>" for i in range(30))
    await page.set_content(
        f"<body style='margin:0'><select aria-label='Raport' style='width:220px'>{options}</select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)

    rec = Recorder(page, overlay, open_hold_ms=10)
    # Sampled on both sides of the cursor glide: "before" is what the list looks
    # like when the recorder decides where to send the cursor, "after" is where
    # the cursor actually landed.
    before: list[dict] = []
    after: list[dict] = []
    glide = rec._approach

    async def spy(control, *, ripple=True, click_sound=False):
        before.append(await _list_geometry(page, 25, 0.0))
        # The box/centre the real glide measures is what a still capture
        # annotates, so the spy has to hand it back rather than swallow it.
        landed = await glide(control, ripple=ripple, click_sound=click_sound)
        after.append(await _list_geometry(page, 25, overlay.pos[1]))
        return landed

    rec._approach = spy  # type: ignore[method-assign]

    await rec.select(RoleTarget(role="combobox", name="Raport"), "Opcja 25")

    assert await page.locator("select").input_value() == "Opcja 25"
    assert len(before) == 2  # one glide per beat
    assert before[0]["open"] is False  # beat 1: the list is still closed
    # the list was scrolled to the option *before* the cursor set off toward it
    assert before[1]["open"] is True
    assert before[1]["scrollTop"] > 0
    # ...and the cursor landed on the row, inside the list's visible box
    landed = after[1]
    assert landed["listTop"] - 1 <= landed["rowTop"]
    assert landed["rowBottom"] <= landed["listBottom"] + 1
    assert landed["listTop"] <= landed["cursorY"] <= landed["listBottom"]


_LIST_GEOMETRY_JS = """([index, cursorY]) => {
  const list = document.querySelector("[data-guidebot-select-list]");
  if (!list || getComputedStyle(list).display === "none") {
    return { open: false, cursorY: cursorY };
  }
  const row = list.querySelector(`[data-guidebot-option-index="${index}"]`);
  const lr = list.getBoundingClientRect();
  const rr = row.getBoundingClientRect();
  return {
    open: true,
    cursorY: cursorY,
    scrollTop: list.scrollTop,
    listTop: lr.top,
    listBottom: lr.bottom,
    rowTop: rr.top,
    rowBottom: rr.bottom,
  };
}"""


async def _list_geometry(page: Page, index: int, cursor_y: float) -> dict:
    return await page.evaluate(_LIST_GEOMETRY_JS, [index, cursor_y])


# --- a widget the page enhanced itself -------------------------------------


async def test_enhanced_widget_is_driven_through_its_own_control_and_list(page):
    overlay = Overlay()
    await page.set_content(_enhanced(["Alfa", "Beta"], ["Alfa", "Beta"]))
    await overlay.install(page)
    await _install_selects(page)
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "Beta")

    assert await page.locator("#s").input_value() == "Beta"
    assert events == ["click", "click"]
