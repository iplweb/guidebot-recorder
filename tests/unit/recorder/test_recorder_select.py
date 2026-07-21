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
from guidebot_recorder.selects.visibility import shape_prelude

# Body plus the shared "already enhanced?" predicate the Python controller
# prepends in production (``selects/visibility.py``).
SELECTS_JS = shape_prelude() + files("guidebot_recorder.selects").joinpath("selects.js").read_text(
    "utf-8"
)


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


_KEYDOWN_SPY = """() => {
  window.__gbKeys = [];
  document.addEventListener("keydown", (event) => { window.__gbKeys.push(event.key); }, true);
}"""


async def _keys(page: Page) -> list[str]:
    return await page.evaluate("() => window.__gbKeys")


async def test_native_with_overlay_sets_the_value_without_stepping(page):
    """`mode: native` travels the cursor to the control and sets the value at once.

    Measured with this repo's pinned Playwright (headless and headed): pressing
    `ArrowDown` on a focused native `<select>` never moves `selectedIndex` and
    never fires `change`. The old stepping loop was theatre — every press a
    no-op, the value landing only via its own final `select_option` guard — so
    there is nothing left to animate: no arrow presses, no `key` SFX.
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


# --- beat 2 verifies the click actually chose the option --------------------
# The branch exists so a run that would produce an unwatchable video fails
# loudly. A beat 2 that ends at `row.click()` without reading the select back
# does the opposite on every path where the click lands on the wrong node: the
# value never changes and `Recorder.select` returns success.


_DISABLED_OPTION_SELECT = (
    "<body style='margin:0'>"
    "<select aria-label='Raport' style='width:220px'>"
    "<option>lista</option><option disabled>tabela</option><option>BibTeX</option>"
    "</select></body>"
)


async def test_a_disabled_option_on_a_shimmed_select_fails_instead_of_doing_nothing(page):
    """A disabled row is visible — `opacity: .45` — and refuses the click.

    Both `onListClick` and `choose` return early for a disabled row, so the
    click lands, nothing happens, and the value stays where it was. Waiting for
    the row to be *visible* cannot tell the two apart, so the step used to
    report success on a select the viewer watches not change.
    """

    overlay = Overlay()
    await page.set_content(_DISABLED_OPTION_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert "tabela" in str(excinfo.value)
    assert await page.locator("select").input_value() == "lista"  # never changed


def _enhanced_with_decoy(labels: list[str], rows: list[str], decoy: str) -> str:
    """The page-widget pattern plus a live region that echoes the option label.

    The decoy is prepended to ``<body>``, so it precedes the widget's own list
    in document order — which is exactly the tie-break the "appeared after the
    click" heuristic applies. A toast, an aria-live region or a "current
    selection" readout is an everyday piece of a dropdown widget.
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
        "  const toast = document.createElement('div');"
        "  toast.id = 'toast';"
        f"  toast.textContent = {json.dumps(decoy)};"
        "  toast.style.cssText = 'width:200px;height:20px';"
        "  document.body.prepend(toast);"
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


async def test_a_decoy_node_carrying_the_option_label_fails_instead_of_reporting_success(page):
    """The page-widget path clicks the first *newly added* node with that text.

    A toast that echoes the label wins the document-order tie-break over the
    real row, so the pointer lands on it, the widget never commits anything and
    the select keeps its old value.
    """

    overlay = Overlay()
    await page.set_content(_enhanced_with_decoy(["Alfa", "Beta"], ["Alfa", "Beta"], "Beta"))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    message = str(excinfo.value)
    assert "Beta" in message
    assert "Alfa" in message  # says what *is* selected, not only what was asked for
    assert await page.locator("#s").input_value() == "Alfa"


def _enhanced_losing_the_snapshot(labels: list[str], rows: list[str]) -> str:
    """The page-widget pattern where beat 1 takes the pre-click snapshot with it.

    ``window.__guidebot_select_snapshot`` is what tells a freshly rendered
    option row from text that was already on the page; a beat 1 that navigates
    or replaces the document drops it. The static ``#decoy`` below carries the
    option label and existed *before* the click, so it is precisely what the
    snapshot exists to exclude.
    """

    return (
        "<body style='margin:0'>"
        f"<div id='decoy' style='width:200px;height:20px'>{rows[-1]}</div>"
        "<select id='s' data-testid='s' style='display:none'>"
        + "".join(f"<option>{label}</option>" for label in labels)
        + "</select>"
        "<div data-testid='w' id='w' style='width:200px;height:30px;border:1px solid #000'>"
        f"{labels[0]}</div>"
        "<script>"
        "document.getElementById('w').addEventListener('click', () => {"
        # exactly what a beat 1 that navigates or replaces the document does
        "  delete window.__guidebot_select_snapshot;"
        "  const list = document.createElement('div');"
        "  list.style.cssText = 'position:fixed;top:120px;left:0;width:200px;background:#fff';"
        f"  for (const label of {json.dumps(rows)}) {{"
        "    const row = document.createElement('div');"
        "    row.textContent = label;"
        "    row.style.cssText = 'padding:4px';"
        "    row.addEventListener('click', () => {"
        "      const sel = document.getElementById('s');"
        "      sel.value = label;"
        "      sel.dispatchEvent(new Event('change', {bubbles: true}));"
        "    });"
        "    list.appendChild(row);"
        "  }"
        "  document.body.appendChild(list);"
        "});"
        "</script></body>"
    )


async def test_a_lost_pre_click_snapshot_is_a_hard_error_not_a_wildcard_match(page):
    """Without the snapshot the "appeared after" filter evaporates.

    Every node on the page then qualifies, so the document-order scan hands back
    whatever carries the label — here a static decoy that was on screen all
    along — and clicking it changes nothing.
    """

    overlay = Overlay()
    await page.set_content(_enhanced_losing_the_snapshot(["Alfa", "Beta"], ["Alfa", "Beta"]))
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    assert "Beta" in str(excinfo.value)
    assert await page.locator("#s").input_value() == "Alfa"


async def test_a_listbox_click_that_selects_nothing_is_not_reported_as_success(page):
    """The one-beat path needs the same read-back as the two-beat ones.

    A page that cancels the click on an ``<option>`` leaves the listbox exactly
    as it was, and there is no exception anywhere to notice it.
    """

    overlay = await _listbox_page(page, "multiple size='3'", ["zwykłe", "pilne", "archiwalne"])
    await page.evaluate(
        "() => document.querySelector('select').addEventListener("
        "'mousedown', (event) => event.preventDefault(), true)"
    )
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "pilne")

    assert "pilne" in str(excinfo.value)
    assert await page.evaluate(_SELECTED_JS) == []


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


# --- a natively-visible listbox (`multiple` / `size > 1`) -------------------
# The shim deliberately skips these: they already draw their option list in the
# page, with no OS popup to replace. The list being on screen already is exactly
# what lets the cursor travel to an `<option>` and click it — measured in this
# repo's pinned Chromium (149.0.7827.55, headless and headed): a plain left
# click on an `<option>` inside a `multiple` / `size > 1` select selects it and
# fires `change`, and `scrollIntoView` on the option scrolls the listbox itself.


def _listbox(attrs: str, labels: list[str], selected: str | None = None) -> str:
    options = "".join(
        f"<option{' selected' if label == selected else ''}>{label}</option>" for label in labels
    )
    return (
        f"<body style='margin:0'><select id='s' data-testid='s' aria-label='Tagi' "
        f"{attrs} style='width:220px'>{options}</select></body>"
    )


_SELECTED_JS = "() => [...document.querySelector('select').selectedOptions].map((o) => o.label)"


async def _listbox_page(page: Page, attrs: str, labels: list[str], selected=None) -> Overlay:
    overlay = Overlay()
    await page.set_content(_listbox(attrs, labels, selected))
    await overlay.install(page)
    await _install_selects(page)
    await page.evaluate(_MOUSEDOWN_SPY)
    return overlay


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


# --- which situation the failure is in --------------------------------------


async def test_error_for_a_hidden_select_blames_the_missing_stand_in_widget(page):
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

    message = str(excinfo.value)
    assert "nie znaleziono widocznej kontrolki" in message
    assert "ukryła" in message  # names *why* there is nothing to click


async def test_error_for_a_visible_unshimmed_select_says_the_shim_skipped_it(page):
    """A visible select the shim declined (marker class) is not a page-widget case.

    The old message claimed no visible control existed, which is plainly false —
    the control is right there; what is missing is the DOM list.
    """

    overlay = Overlay()
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' "
        "class='select2-hidden-accessible' style='width:220px'>"
        "<option>Alfa</option><option>Beta</option></select></body>"
    )
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    message = str(excinfo.value)
    assert "select#s" in message
    assert "Beta" in message
    assert "ukryła" not in message  # this select is *not* hidden
    assert "mode: native" in message
    # ...and it names the marker class that actually caused it, which is the one
    # thing the author can act on. Reading "the shim declined it" off geometry
    # alone could never say this.
    assert "select2-hidden-accessible" in message


async def test_a_visible_select_with_no_shim_in_the_context_blames_the_missing_shim(
    page, monkeypatch
):
    """`installed: false` is not "the page enhanced it" — it is "there is no shim here".

    This is `config.selects.mode: native` plus a per-step `mode: shim`, and the
    bare contexts (health probe, unit-test page) besides. The old code sent it
    straight to the association heuristic, which always finds *something*: the
    cursor clicked an unrelated sibling on camera, the diagnosis blamed the
    option, and it burned the whole option wait to get there.
    """

    monkeypatch.setattr(recorder_module, "OPTION_WAIT_MS", 400)
    overlay = Overlay()
    await page.set_content(
        "<body style='margin:0'><select id='s' data-testid='s' style='width:220px'>"
        "<option>Alfa</option><option>Beta</option></select>"
        "<span class='hint' style='width:200px;height:20px;display:block'>podpowiedź</span>"
        "</body>"
    )
    await overlay.install(page)
    # deliberately no `_install_selects`: there is no shim layer in this context
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(TestidTarget(testid="s"), "Beta")

    message = str(excinfo.value)
    assert "select#s" in message
    assert "mode: native" in message  # names the configuration, not the option
    assert "nie pojawiła się" not in message  # not "the option never showed up"
    assert overlay.pos == (0.0, 0.0)  # the cursor never set off towards the hint
    assert await page.locator("#s").input_value() == "Alfa"


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
