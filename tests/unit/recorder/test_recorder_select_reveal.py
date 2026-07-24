"""The still-capture hook: ``on_revealed`` photographs the one documentable instant.

``on_revealed`` exists so the PDF guide can photograph the one instant a dropdown
is worth documenting: list open, cursor on the row, nothing chosen yet.
Everything here asserts that *instant*, not that the hook was called.

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
    _install_selects,
    _listbox_page,
    _raw_page,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


_LIST_OPEN_JS = """() => {
  const list = document.querySelector("[data-guidebot-select-list]");
  return !!list && getComputedStyle(list).display !== "none";
}"""


async def test_on_revealed_fires_once_while_the_list_is_open_and_before_the_choice(page):
    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)
    seen: list[dict] = []

    async def hook(reveal):
        seen.append(
            {
                "reveal": reveal,
                "value": await page.locator("select").input_value(),
                "open": await page.evaluate(_LIST_OPEN_JS),
                "row": await page.locator(
                    '[data-guidebot-select-list] [data-guidebot-option-index="2"]'
                ).bounding_box(),
            }
        )

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", on_revealed=hook)

    assert len(seen) == 1
    moment = seen[0]
    assert moment["open"] is True  # the list is unfurled...
    assert moment["value"] == "lista"  # ...and nothing has been chosen yet
    # The geometry handed out is the row's own, measured from the live DOM — not
    # the collapsed control's, which is what a frame taken after the click shows.
    assert moment["reveal"].row_box == moment["row"]
    cx, cy = moment["reveal"].row_center
    assert moment["row"]["x"] <= cx <= moment["row"]["x"] + moment["row"]["width"]
    assert moment["row"]["y"] <= cy <= moment["row"]["y"] + moment["row"]["height"]
    # The shim leaves the <select> in place, so it is the control to frame.
    assert moment["reveal"].control_box == await page.locator("select").bounding_box()
    assert await page.locator("select").input_value() == "BibTeX"  # and the step completes


async def test_on_revealed_frames_the_page_widget_not_the_hidden_select(page):
    """A `display: none` original has no box, so framing it would mark nothing."""

    overlay = Overlay()
    await page.set_content(_enhanced(["Alfa", "Beta"], ["Alfa", "Beta"]))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)
    seen: list = []

    async def hook(reveal):
        seen.append(reveal)

    await rec.select(TestidTarget(testid="s"), "Beta", on_revealed=hook)

    assert len(seen) == 1
    assert await page.locator("#s").bounding_box() is None
    assert seen[0].control_box == await page.locator("#w").bounding_box()
    # The widget's list is drawn at `top: 120px`, so a row from it can only come
    # from below there — the widget itself sits at the top of the body.
    assert seen[0].row_box is not None and seen[0].row_box["y"] >= 120


async def test_on_revealed_for_a_listbox_frames_the_listbox_and_marks_the_row(page):
    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne", "archiwalne"])
    rec = Recorder(page, overlay, open_hold_ms=10)
    seen: list = []

    async def hook(reveal):
        seen.append(reveal)

    await rec.select(TestidTarget(testid="s"), "pilne", on_revealed=hook)

    assert len(seen) == 1
    assert seen[0].control_box == await page.locator("#s").bounding_box()
    assert seen[0].row_box == await page.locator("#s option").nth(1).bounding_box()


async def test_native_reveals_the_collapsed_control_and_no_row(page):
    """`mode: native` has no list to unfurl, and that must not become an error."""

    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)
    seen: list = []

    async def hook(reveal):
        seen.append(reveal)

    await rec.select(
        RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True, on_revealed=hook
    )

    assert len(seen) == 1
    assert seen[0].row_box is None and seen[0].row_center is None
    assert seen[0].control_box == await page.locator("select").bounding_box()
    assert await page.locator("select").input_value() == "BibTeX"


async def test_a_hook_that_raises_leaves_the_choice_unmade(page):
    """The hook runs before the commit, so a failure there must not half-apply."""

    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    async def hook(_reveal):
        raise RuntimeError("zrzut ekranu się nie udał")

    with pytest.raises(RuntimeError, match="zrzut ekranu"):
        await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", on_revealed=hook)

    assert await page.locator("select").input_value() == "lista"


async def test_ripple_false_keeps_the_click_ring_out_of_a_still_frame(page):
    """A PDF page would freeze the ring mid-animation, so the guide turns it off."""

    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, on_sfx=lambda _name: None, open_hold_ms=10)
    rings: list[dict] = []
    original = overlay.ripple

    async def spy(pg, **kwargs):
        rings.append(kwargs)
        await original(pg, **kwargs)

    overlay.ripple = spy  # type: ignore[method-assign]

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", ripple=False)

    assert rings == []
    assert await page.locator("select").input_value() == "BibTeX"
    assert overlay.pos != (0.0, 0.0)  # the cursor still travelled
