"""Which refusals mean "the option is not on offer" — ``SelectDriveError.reason``.

``SelectDriveError.reason`` is the only thing that separates a step an
``optional: true`` author asked to be shrugged off from a step that is simply
broken, so every raise site is pinned here rather than left to whichever caller
happens to read it. The rule is one sentence: ``OPTION_MISSING`` means the
control does not carry that label, and *nothing else* does.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import (
    OPTION_MISSING,
    UNDRIVABLE,
    Recorder,
    SelectDriveError,
)

# ``OPTION_WAIT_MS`` is patched below purely for speed (400 ms instead of 5 s) by
# ``test_a_widget_that_never_draws_the_row_is_not_option_missing``, which then
# asserts on a `reason`; ``require_option`` is spied on to prove one guard
# settles both cases. Both are patched on the module whose globals the driver
# reads at call time — the timeout on ``select.driver``, the guard on
# ``select.probe`` — because naming the wrong module (or importing by name) would
# rebind something nobody consults: the test would still pass and simply run
# seconds slower, or count nothing. ``test_recorder_seams.py`` makes that a failure.
from guidebot_recorder.recorder.select import driver as select_driver
from guidebot_recorder.recorder.select import probe as select_probe

from ._recorder_select_helpers import (
    _DISABLED_OPTION_SELECT,
    _RAW_SELECT,
    _SELECTED_JS,
    _enhanced,
    _enhanced_with_decoy,
    _install_selects,
    _listbox_page,
    _raw_page,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


async def test_a_label_the_shimmed_select_does_not_carry_is_option_missing(page):
    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "nie ma takiej")

    assert excinfo.value.reason == OPTION_MISSING


async def test_a_label_the_listbox_does_not_carry_is_option_missing(page):
    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne"])
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "nie ma takiej")

    assert excinfo.value.reason == OPTION_MISSING


async def test_a_page_widget_whose_select_lost_the_option_is_option_missing(page):
    """The widget still draws the row; the `<select>` behind it no longer offers it.

    A page widget keeps the original's `<option>` elements — that is what the
    form submits — so the underlying select is the honest answer to "is this
    option on offer?", and asking it is what lets this case be told apart from
    the one below.
    """

    overlay = Overlay()
    await page.set_content(_enhanced(["Alfa"], ["Alfa", "Beta"]))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert excinfo.value.reason == OPTION_MISSING


async def test_a_widget_that_never_draws_the_row_is_not_option_missing(page, monkeypatch):
    """The option *is* on offer — the widget failed to render it. That is a bug.

    The mirror image of the test above, and the reason the classification asks
    the `<select>` rather than reading the timeout as "no such option": here a
    caller must not skip, because the step it was told to perform is performable
    and simply did not happen.
    """

    monkeypatch.setattr(select_driver, "OPTION_WAIT_MS", 400)
    overlay = Overlay()
    await page.set_content(_enhanced(["Alfa", "Beta"], ["Alfa"]))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_shim_removed_mid_step_is_not_option_missing(page):
    """The label is spelled perfectly; the page took the control over mid-step."""

    overlay = Overlay()
    await page.set_content(_RAW_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    await page.evaluate(
        """() => {
      document.querySelector("select").addEventListener("mousedown", () => {
        document.querySelector("select").classList.add("select2-hidden-accessible");
      }, true);
    }"""
    )
    rec = Recorder(page, overlay, open_hold_ms=300)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_disabled_option_is_not_option_missing(page):
    """`disabled` means the option is there and refuses to be chosen."""

    overlay = Overlay()
    await page.set_content(_DISABLED_OPTION_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_click_that_did_not_take_is_not_option_missing(page):
    """The decoy case: the cursor landed on a node that only echoes the label."""

    overlay = Overlay()
    await page.set_content(_enhanced_with_decoy(["Alfa", "Beta"], ["Alfa", "Beta"], "Beta"))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_control_with_nothing_to_unfurl_is_not_option_missing(page):
    """A hidden select with no stand-in offers the option; it cannot be driven."""

    overlay = Overlay()
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' style='display:none'>"
        "<option>Alfa</option><option>Beta</option></select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_disabled_option_on_the_compile_path_is_not_option_missing(page):
    """The listless direct set now refuses up front — and must refuse *loudly*.

    Both halves are the point. A `disabled` option is present, correctly spelled
    and on offer, so classifying it as `OPTION_MISSING` would make an
    `optional: true` step skip a control the page deliberately locked — the
    guide would quietly stop covering it and nobody would learn why. And a raw
    `playwright.TimeoutError` (what this used to be) carries no `reason` at all,
    so it could not be classified either way.
    """

    page.set_default_timeout(3000)
    await page.set_content(_DISABLED_OPTION_SELECT)
    await _install_selects(page)
    rec = Recorder(page, overlay=None)  # compile path

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_disabled_option_under_native_mode_is_not_option_missing(page):
    """The other listless path, through the `mode: native` escape hatch."""

    page.set_default_timeout(3000)
    overlay = Overlay()
    await page.set_content(_DISABLED_OPTION_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela", native=True)

    assert excinfo.value.reason == UNDRIVABLE


async def test_a_disabled_option_in_a_listbox_is_not_option_missing(page):
    """The listbox path asks the same guard as everyone else, so it agrees.

    It used to check only "is the label there?" and then click the row, which a
    disabled `<option>` silently refuses — caught one beat later by
    `probe.confirm_selected`, and named by a message about the cursor missing
    rather than about the option being locked. Routing it through
    `probe.require_option`
    gives it the same verdict, the same wording and the same `reason` the other
    three paths carry.
    """

    overlay = Overlay()
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' multiple size='3'>"
        "<option>zwykłe</option><option disabled>pilne</option>"
        "<option>archiwalne</option></select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "pilne")

    assert excinfo.value.reason == UNDRIVABLE
    assert "wyłączona" in str(excinfo.value)
    assert await page.evaluate(_SELECTED_JS) == []  # never touched


async def test_a_hidden_listbox_is_not_option_missing(page):
    """The listbox has no box; nothing was learned about which options it offers.

    An `optional:` step must fail here, not skip: a control an earlier step hid
    (or a compiled target the page's layout drifted away from) is a broken
    scenario, and the option it names may be sitting right there.
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

    assert excinfo.value.reason == UNDRIVABLE


async def test_one_guard_answers_can_this_option_be_chosen(page, monkeypatch):
    """Both refusals come from a single call, so they cannot drift apart.

    The merge that produced this file had two candidate fast-fails for the two
    listless direct sets — one for "no such option", one for "the option is
    `disabled`" — each with its own JS and its own label-matching loop. Two
    definitions of the same question is how this module previously grew four
    disagreeing answers to "is this select enhanced?". Pinned here: one
    `probe.require_option` per drive, and it settles both cases.

    The spy replaces the function on `select.probe` rather than on the recorder,
    because that is the module whose globals the driver reads when it makes the
    call — patching anywhere else would leave the real guard running and count
    nothing.
    """

    calls: list[str] = []
    await page.set_content(_DISABLED_OPTION_SELECT)
    await _install_selects(page)
    rec = Recorder(page, overlay=None)  # compile path
    original = select_probe.require_option

    async def _spy(locator, option):
        calls.append(option)
        return await original(locator, option)

    monkeypatch.setattr(select_probe, "require_option", _spy)

    with pytest.raises(SelectDriveError) as disabled:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")
    with pytest.raises(SelectDriveError) as absent:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "nie ma takiej")

    assert calls == ["tabela", "nie ma takiej"]  # one guard, one call each
    assert disabled.value.reason == UNDRIVABLE
    assert absent.value.reason == OPTION_MISSING
