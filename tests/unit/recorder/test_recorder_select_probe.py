"""The compile-path drivability probe: fail before touching the value.

On the compile path (no overlay) an enhanced ``<select>`` with no stand-in
control cannot be driven, so the probe refuses up front rather than setting a
value the recorded video will never show being chosen. The probe is skipped
where it is moot: a shimmed select the recorder can open itself, and ``native``
mode which sets the value directly.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError

from ._recorder_select_helpers import _RAW_SELECT, _enhanced, _install_selects, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


async def test_compile_probe_fails_for_an_enhanced_select_with_no_control(page):
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' style='display:none'>"
        "<option>Alfa</option><option>Beta</option></select></body>"
    )
    await _install_selects(page)
    rec = Recorder(page, overlay=None)  # compile mode

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert "Beta" in str(excinfo.value)
    assert await page.locator("#s").input_value() == "Alfa"  # value untouched


async def test_compile_probe_passes_for_an_enhanced_select_with_a_control(page):
    await page.set_content(_enhanced(["Alfa", "Beta"], ["Alfa", "Beta"]))
    await _install_selects(page)
    rec = Recorder(page, overlay=None)

    await rec.select(TestidTarget(testid="s"), "Beta")

    assert await page.locator("#s").input_value() == "Beta"


async def test_compile_probe_is_skipped_for_a_shimmed_select(page):
    await page.set_content(_RAW_SELECT)
    await _install_selects(page)
    rec = Recorder(page, overlay=None)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert await page.locator("select").input_value() == "tabela"


async def test_compile_probe_is_skipped_for_native_mode(page):
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' style='display:none'>"
        "<option>Alfa</option><option>Beta</option></select></body>"
    )
    await _install_selects(page, mode="native")
    rec = Recorder(page, overlay=None)

    await rec.select(TestidTarget(testid="s"), "Beta", native=True)

    assert await page.locator("#s").input_value() == "Beta"
