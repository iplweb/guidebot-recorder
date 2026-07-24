"""A natively-visible listbox (`multiple` / `size > 1`): one beat, no shim.

The shim deliberately skips these: they already draw their option list in the
page, with no OS popup to replace. The list being on screen already is exactly
what lets the cursor travel to an ``<option>`` and click it — measured in this
repo's pinned Chromium (149.0.7827.55, headless and headed): a plain left click
on an ``<option>`` inside a ``multiple`` / ``size > 1`` select selects it and
fires ``change``, and ``scrollIntoView`` on the option scrolls the listbox
itself.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError

from ._recorder_select_helpers import (
    _SELECTED_JS,
    _hits,
    _install_selects,
    _listbox,
    _listbox_page,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


async def test_multiple_listbox_option_is_clicked_where_it_already_is(page):
    """One beat, not two: the list is already on screen, so the cursor goes to the row."""

    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne", "archiwalne"])
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "pilne")

    assert await page.evaluate(_SELECTED_JS) == ["pilne"]
    assert await _hits(page) == ["option"]  # the pointer landed on the row itself
    assert events == ["click"]  # a single visible beat
    assert overlay.pos != (0.0, 0.0)  # the cursor really travelled there


async def test_size_gt_one_listbox_option_is_clicked_where_it_already_is(page):
    overlay = await _listbox_page(page, "size='3'", ["lista", "tabela", "BibTeX"])
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "BibTeX")

    assert await page.evaluate(_SELECTED_JS) == ["BibTeX"]
    assert await _hits(page) == ["option"]


async def test_listbox_click_fires_change_exactly_once(page):
    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne", "archiwalne"])
    await page.evaluate(
        "() => { window.__gbChanges = [];"
        " document.querySelector('select').addEventListener('change', () =>"
        " window.__gbChanges.push([...document.querySelector('select').selectedOptions]"
        ".map((o) => o.label))); }"
    )
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "archiwalne")

    assert await page.evaluate("() => window.__gbChanges") == [["archiwalne"]]


async def test_listbox_selection_replaces_the_previous_one_exactly_like_select_option(page):
    """The one semantic that must not drift: what happens to the *other* options.

    Measured on the pinned Chromium: `select_option(label=…)` on a multi-select
    deselects everything else, and so does a plain click on an `<option>`. This
    step means "pick this one", and it meant that before the branch too.
    """

    overlay = await _listbox_page(
        page, "multiple size='3'", ["zwykłe", "pilne", "archiwalne"], selected="zwykłe"
    )
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "archiwalne")

    assert await page.evaluate(_SELECTED_JS) == ["archiwalne"]


async def test_listbox_is_scrolled_to_the_option_before_the_cursor_glides(page):
    """A row below the fold of the listbox is scrolled into it, then clicked."""

    labels = [f"Opcja {i}" for i in range(40)]
    overlay = await _listbox_page(page, "multiple size='5'", labels)
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "Opcja 33")

    assert await page.evaluate(_SELECTED_JS) == ["Opcja 33"]
    landed = await page.evaluate(
        """(cursorY) => {
          const sel = document.querySelector('select');
          const row = sel.options[33];
          const sr = sel.getBoundingClientRect();
          const rr = row.getBoundingClientRect();
          return {scrollTop: sel.scrollTop, selTop: sr.top, selBottom: sr.bottom,
                  rowTop: rr.top, rowBottom: rr.bottom, cursorY};
        }""",
        overlay.pos[1],
    )
    assert landed["scrollTop"] > 0  # the listbox scrolled, not the page alone
    assert landed["selTop"] - 1 <= landed["rowTop"]
    assert landed["rowBottom"] <= landed["selBottom"] + 1
    assert landed["selTop"] <= landed["cursorY"] <= landed["selBottom"]


async def test_listbox_is_never_shimmed_and_grows_no_overlay(page):
    """The non-goal still holds: no button, no DOM list, no re-parenting."""

    await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne"])
    rec = Recorder(page, None, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "pilne")

    assert await page.evaluate(
        """() => ({
          shimmed: window.__guidebot_selects.isShimmed(document.querySelector('select')),
          overlays: document.querySelectorAll(
            '[data-guidebot-select-button],[data-guidebot-select-list]').length,
          parent: document.querySelector('select').parentElement.tagName,
        })"""
    ) == {"shimmed": False, "overlays": 0, "parent": "BODY"}


async def test_unknown_option_in_a_listbox_raises_naming_the_option(page):
    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne"])
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "nie ma takiej")

    assert "nie ma takiej" in str(excinfo.value)
    assert await page.evaluate(_SELECTED_JS) == []  # no silent fallback


async def test_compile_path_sets_a_listbox_without_probing_for_a_page_widget(page):
    """The regression itself: no overlay, no page widget, and it must still work."""

    await page.set_content(_listbox("multiple size='3'", ["zwykłe", "pilne"]))
    await _install_selects(page)
    rec = Recorder(page, overlay=None)

    await rec.select(TestidTarget(testid="s"), "pilne")

    assert await page.evaluate(_SELECTED_JS) == ["pilne"]


async def test_native_mode_still_works_on_a_listbox(page):
    """The old workaround must keep working for scenarios that already use it."""

    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne"])
    rec = Recorder(page, overlay, open_hold_ms=10)

    await rec.select(TestidTarget(testid="s"), "pilne", native=True)

    assert await page.evaluate(_SELECTED_JS) == ["pilne"]


async def test_a_hidden_listbox_fails_fast_instead_of_a_raw_timeout(page):
    """A `display: none` listbox — e.g. reached via a stale compiled target —
    used to hang out a full step timeout instead of failing legibly.

    The `listbox` shape (`multiple` / `size > 1`) needs no shim stand-in when
    visible, so `_select_in_listbox` drove straight at the `<option>` — and
    when the select itself has no box at all, neither `_approach`'s bounding
    box read nor `Locator.click()`'s own actionability wait ever resolves.
    Measured on this branch before the fix: with a short default timeout, this
    raised a raw `playwright.async_api.TimeoutError` ("element is not
    visible") instead of `SelectDriveError`.
    """

    page.set_default_timeout(3000)
    overlay = Overlay()
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' aria-label='Tagi' "
        "multiple size='3' style='display:none'>"
        "<option>zwykłe</option><option>pilne</option><option>archiwalne</option>"
        "</select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "pilne")

    message = str(excinfo.value)
    assert "pilne" in message
    assert "compile --force" in message
    assert await page.evaluate(_SELECTED_JS) == []  # never touched
