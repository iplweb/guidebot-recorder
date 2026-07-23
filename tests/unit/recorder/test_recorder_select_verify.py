"""Beat 2 verifies the click actually chose the option, and names what went wrong.

The branch exists so a run that would produce an unwatchable video fails loudly.
A beat 2 that ends at ``row.click()`` without reading the select back does the
opposite on every path where the click lands on the wrong node: the value never
changes and ``Recorder.select`` returns success. The second half of the file
(``which situation the failure is in``) is about *wording* — a hidden select, a
shim the page declined, and a context with no shim at all each want a different
sentence.

Split out of ``test_recorder_select.py``; see ``_recorder_select_helpers.py`` for
the family map and the shared page/session scaffolding.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError

# ``OPTION_WAIT_MS`` is patched below purely for speed (400 ms instead of 5 s) by
# the two tests that then assert on a *message*. It is patched on the module
# whose globals the waiting code reads at call time — ``select.driver`` — because
# naming the wrong module (or importing the constant by name) would rebind
# something nobody consults: the tests would still pass and simply take five
# seconds longer per patched test. ``test_recorder_seams.py`` makes that a failure.
from guidebot_recorder.recorder.select import driver as select_driver

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


# --- beat 2 verifies the click actually chose the option --------------------


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


async def test_a_disabled_option_at_compile_time_fails_fast_instead_of_a_raw_timeout(page):
    """The direct path (no overlay) used to hang out a full step timeout.

    `Locator.select_option` retries "waiting for element to be visible and
    enabled" — Chromium never considers a `disabled` `<option>` selectable, so
    the retry loop never ends on its own and `select_option` raises nothing
    until the caller's own timeout elapses, as a raw English
    `playwright.TimeoutError` naming no control and no option. Measured on this
    branch before the fix: with `page.set_default_timeout(...)` at 8s, this
    test took the full 8s and raised `playwright.async_api.TimeoutError`
    instead of `SelectDriveError`.
    """

    page.set_default_timeout(3000)
    await page.set_content(_DISABLED_OPTION_SELECT)
    await _install_selects(page)
    rec = Recorder(page, overlay=None)  # compile path

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela")

    assert "tabela" in str(excinfo.value)
    assert "wyłączona" in str(excinfo.value)
    assert await page.locator("select").input_value() == "lista"  # never changed


async def test_a_disabled_option_under_native_mode_fails_fast_instead_of_a_raw_timeout(page):
    """The `mode: native` escape hatch shares the same direct `select_option` call.

    Same hazard as the compile path, reached instead through the per-step
    `native: true` override with an overlay installed (the render-time shape of
    the hatch).
    """

    page.set_default_timeout(3000)
    overlay = Overlay()
    await page.set_content(_DISABLED_OPTION_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    rec = Recorder(page, overlay, open_hold_ms=10)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "tabela", native=True)

    assert "tabela" in str(excinfo.value)
    assert "wyłączona" in str(excinfo.value)
    assert await page.locator("select").input_value() == "lista"  # never changed


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
    monkeypatch.setattr(select_driver, "OPTION_WAIT_MS", 400)
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


async def test_a_select_unshimmed_between_the_beats_says_so_instead_of_blaming_the_option(
    page,
):
    """The two failures behind beat 2 want different fixes, so they get different words.

    A select the page enhances late (select2 hydrating on first interaction)
    loses its shim between beat 1 and beat 2: the marker class appears, the
    observer unshims, and the rows the recorder was about to click are gone.
    The option is still on the ``<select>`` — saying "the list does not contain
    it" sends the author looking for a typo in a label that is spelled
    perfectly. The message has to name the event that actually happened.
    """

    overlay = Overlay()
    await page.set_content(_RAW_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    # select2 hydrating on the first interaction: the marker class lands during
    # beat 1, and the next classification pass unshims underneath the recorder.
    await page.evaluate(
        """() => {
      document.querySelector("select").addEventListener("mousedown", () => {
        document.querySelector("select").classList.add("select2-hidden-accessible");
      }, true);
    }"""
    )
    # Long enough for the (20 ms) settle window to run a pass between the beats.
    rec = Recorder(page, overlay, open_hold_ms=300)

    with pytest.raises(SelectDriveError) as excinfo:
        await rec.select(RoleTarget(role="combobox", name="Raport"), "BibTeX")

    message = str(excinfo.value)
    assert "nie zawiera opcji" not in message  # the option was never the problem
    assert "select2-hidden-accessible" in message  # what the page did instead
    assert "BibTeX" in message


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

    monkeypatch.setattr(select_driver, "OPTION_WAIT_MS", 400)
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
