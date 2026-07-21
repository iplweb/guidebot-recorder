"""Render-time choreography for ``select:`` steps (spec §4).

The two beats — cursor opens the list, cursor clicks the option — are what makes
a dropdown visible on camera at all, so they are asserted through what a viewer
would actually see: where the synthetic cursor travelled, which element received
the pointer, and whether the wanted row was scrolled into the list's viewport
before the cursor set off towards it.

Patterned on ``tests/unit/selects/test_selects_js.py``: a real Chromium page with
``selects.js`` evaluated directly and a short settle window, so the widget is
classified before the recorder drives it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder import recorder as recorder_module
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError

SELECTS_JS = files("guidebot_recorder.selects").joinpath("selects.js").read_text("utf-8")


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        pg = await browser.new_page()
        try:
            yield pg
        finally:
            await browser.close()


async def _install_selects(page: Page, **cfg: object) -> None:
    """Install the widget with a short settle window and await the first pass."""

    merged = {"settleMs": 20, **cfg}
    await page.evaluate(f"window.__guidebot_selects_config = {json.dumps(merged)};")
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")


_MOUSEDOWN_SPY = """() => {
  window.__gbHits = [];
  document.addEventListener("mousedown", (event) => {
    const el = event.target;
    if (!el || !el.getAttribute) return;
    if (el.hasAttribute("data-guidebot-option-index")) {
      window.__gbHits.push("option:" + el.getAttribute("data-guidebot-option-index"));
    } else if (el.hasAttribute("data-guidebot-select-button")) {
      window.__gbHits.push("button");
    } else {
      window.__gbHits.push(el.tagName.toLowerCase());
    }
  }, true);
}"""


async def _hits(page: Page) -> list[str]:
    return await page.evaluate("() => window.__gbHits")


_RAW_SELECT = (
    "<body style='margin:0'>"
    "<select aria-label='Raport' style='width:220px'>"
    "<option>lista</option><option>tabela</option><option>BibTeX</option>"
    "</select></body>"
)


async def _raw_page(page: Page) -> Overlay:
    overlay = Overlay()
    await page.set_content(_RAW_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    await page.evaluate(_MOUSEDOWN_SPY)
    return overlay


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

    async def spy(control, *, click_sound=False):
        before.append(await _list_geometry(page, 25, 0.0))
        await glide(control, click_sound=click_sound)
        after.append(await _list_geometry(page, 25, overlay.pos[1]))

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


# --- the `native` escape hatch ---------------------------------------------


_PLAIN_SELECT = (
    "<select aria-label='Raport'>"
    "<option>lista</option><option>tabela</option><option>BibTeX</option>"
    "</select>"
)


async def test_native_with_overlay_still_steps_the_value_with_arrow_keys(page):
    overlay = Overlay()
    await page.set_content(_PLAIN_SELECT)
    await overlay.install(page)
    events: list[str] = []
    rec = Recorder(page, overlay, on_sfx=events.append)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True)

    assert await page.locator("select").input_value() == "BibTeX"
    assert overlay.pos != (0.0, 0.0)
    # ripple + one arrow key per step (lista → tabela → BibTeX), i.e. still animated
    assert events == ["click", "key", "key"]


async def test_native_without_overlay_takes_the_direct_path(page):
    await page.set_content(_PLAIN_SELECT)
    events: list[str] = []
    rec = Recorder(page, overlay=None, on_sfx=events.append)

    await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX", native=True)

    assert await page.locator("select").input_value() == "BibTeX"
    assert "key" not in events  # no arrow stepping without an overlay


# --- a widget the page enhanced itself -------------------------------------


def _enhanced(labels: list[str], rows: list[str]) -> str:
    """A hidden ``<select>`` plus a sibling widget that opens a body-level list.

    Reproduces the select2 / Tom Select *pattern* rather than vendoring either.
    """

    return (
        "<body style='margin:0'>"
        "<select id='s' data-testid='s' style='display:none'>"
        + "".join(f"<option>{label}</option>" for label in labels)
        + "</select>"
        "<div data-testid='w' id='w' style='width:200px;height:30px;border:1px solid #000'>"
        f"{labels[0]}</div>"
        "<script>"
        "document.getElementById('w').addEventListener('click', () => {"
        "  const list = document.createElement('div');"
        "  list.id = 'fake-list';"
        "  list.style.cssText = 'position:fixed;top:120px;left:0;width:200px;background:#fff';"
        f"  for (const label of {json.dumps(rows)}) {{"
        "    const row = document.createElement('div');"
        "    row.textContent = label;"
        "    row.style.cssText = 'padding:4px';"
        "    row.addEventListener('click', () => {"
        "      const sel = document.getElementById('s');"
        "      sel.value = label;"
        "      sel.dispatchEvent(new Event('change', {bubbles: true}));"
        "      list.remove();"
        "    });"
        "    list.appendChild(row);"
        "  }"
        "  document.body.appendChild(list);"
        "});"
        "</script></body>"
    )


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


async def test_beat_two_without_a_matching_node_raises_naming_the_option(page, monkeypatch):
    monkeypatch.setattr(recorder_module, "OPTION_WAIT_MS", 400)
    overlay = Overlay()
    await page.set_content(_enhanced(["Alfa", "Beta"], ["Alfa"]))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert "Beta" in str(excinfo.value)
    assert await page.locator("#s").input_value() == "Alfa"  # no silent fallback


async def test_unknown_option_on_a_shimmed_select_raises_naming_the_option(page):
    overlay = await _raw_page(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "nie ma takiej")

    assert "nie ma takiej" in str(excinfo.value)


async def test_two_beats_without_any_resolvable_control_raise(page):
    overlay = Overlay()
    # a hidden select with no widget standing in for it: nothing to click
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' "
        "style='display:none'><option>Alfa</option>"
        "<option>Beta</option></select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert "Beta" in str(excinfo.value)


# --- compile-path drivability probe ----------------------------------------


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
