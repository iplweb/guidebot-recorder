"""E2E: the shimmed drop-down, list open at click time, through compile → render.

A native ``<select>`` draws its option list as an OS popup, so the recording
never shows it. The shim replaces that list with a DOM one; pages that enhanced
their own selects (select2, Tom Select) keep theirs and are driven through it.
The fixture reproduces both *patterns* rather than vendoring the libraries.

What makes this suite worth its runtime is not that the value ends up right —
``select_option`` did that before the branch, invisibly. It is that **at the
moment the option row is clicked the list is attached and visible in the DOM**,
which is asserted from inside the browser by :class:`SelectSpy` rather than
inferred from the final video.

Reasoner mocked (deterministic), TTS fake (silent mp3) — no network, no Codex.

Shared scaffolding lives in ``_selects_e2e`` and is imported explicitly; there
is no ``conftest.py`` in this suite by design. The pytest markers do not travel
through that import, so they are re-declared below from ``PYTESTMARK``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile_in_browser
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled

from ._selects_e2e import (
    FIXTURE,
    PYTESTMARK,
    FakeTts,
    SelectReasoner,
    SelectSpy,
    _assert_playable,
    _write,
)

pytestmark = list(PYTESTMARK)


# --- scenarios --------------------------------------------------------------
# ``settleMs: 20`` everywhere the settle window is not what is under test: the
# readiness barrier costs up to one settle window per document, and the fixture
# enhances its selects in inline markup, so it has nothing to wait for.

FOUR_CONTROLS_SCENARIO = """\
config:
  title: Listy rozwijane
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Pokażę, jak wypełnić formularz."
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX"}}
  - select: {{from: "lista województw", option: "Mazowieckie"}}
  - select: {{from: "lista miast", option: "Lublin"}}
  - select: {{from: "lista tagów", option: "pilne"}}
"""

CLICK_SCENARIO = """\
config:
  title: Klik w listę
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Rozwijam listę."
  - navigate: "{url}"
  - click: "lista formatów raportu"
  - wait: 0.4
"""


# --- the four controls ------------------------------------------------------


async def test_four_controls_compile_and_render_with_the_list_open_at_click_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw, fake-select2, fake-Tom-Select and ``multiple`` — all four survive a
    compile + render, the chosen option ends up selected, and for the three
    drop-downs the list is attached and visible **in the click event itself**.

    All four run on the default ``mode: shim``, with no per-step escape hatch
    anywhere: this is the shipped configuration. The ``<select multiple>`` is
    still never shimmed (spec non-goal — it draws no OS popup), but its rows are
    laid out in the page already, so the cursor clicks the ``<option>`` where it
    sits and the click is asserted to have landed inside the listbox's visible
    box.
    """

    path = _write(tmp_path, "selects.scenario.yaml", FOUR_CONTROLS_SCENARIO, FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        reasoner = SelectReasoner()
        await run_compile_in_browser(path, browser, reasoner)
        assert reasoner.calls == 4

        compiled = load_compiled(compiled_path(path))
        frozen = [action for action in compiled.actions if action is not None]
        assert [action.action for action in frozen] == ["select"] * 4
        # The shim never re-parents a select, so every identity is the <select>'s
        # own — including the two the page enhanced itself.
        assert {action.identity.tag for action in frozen} == {"select"}

        spy.reset()  # from here on, assertions are about the render
        out = tmp_path / "selects.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)

    assert [record["option"] for record in spy.selects] == [
        "BibTeX",
        "Mazowieckie",
        "Lublin",
        "pilne",
    ]
    # Every step ended with exactly the wanted option selected.
    for record in spy.selects:
        assert record["selected"] == [record["option"]]

    # Classification, seen live during the render: only the raw select is shimmed.
    assert spy.selects[0]["shims"] == {
        "format": True,
        "woj": False,
        "miasto": False,
        "tagi": False,
    }

    # The three drop-downs: two beats, and at beat 2 the list was really there.
    for record in spy.selects[:3]:
        kinds = [event["kind"] for event in record["events"]]
        assert kinds == ["control", "option"], record
        opened = record["events"][1]
        assert opened["label"] == record["option"]
        assert opened["attached"] is True
        assert opened["visible"] is True
        assert opened["atBodyLevel"] is True  # nothing can clip it
        assert len(opened["labels"]) == 3
        assert record["option"] in opened["labels"]

    raw, select2, tom = spy.selects[:3]
    # The shim leaves the real <select> as the hit target (its button is
    # pointer-events: none), and its list opens downward.
    assert raw["events"][0] == {"kind": "control", "tag": "select", "id": "format"}
    assert raw["events"][1]["shim"] is True
    assert raw["events"][1]["below"] is True
    assert raw["events"][1]["labels"] == ["lista", "tabela", "BibTeX"]
    # The two enhanced controls are driven through the page's own widget and the
    # page's own list — the shim built neither.
    assert select2["events"][0]["id"] == "woj-widget"
    assert select2["events"][1]["shim"] is False
    assert tom["events"][0]["id"] == "miasto-widget"
    assert tom["events"][1]["shim"] is False

    # <select multiple>: the default mode, no escape hatch, one visible beat —
    # the pointer landed on the <option> itself, inside the listbox's own box.
    listbox = spy.selects[3]
    assert listbox["native"] is False
    assert listbox["events"] == [
        {
            "kind": "listboxOption",
            "label": "pilne",
            "visible": True,
            "insideListbox": True,
        }
    ]
    # ...and it stayed unshimmed throughout: no button, no DOM list of ours.
    assert listbox["shims"]["tagi"] is False


# --- a click: step aimed at a shimmed select --------------------------------


async def test_click_step_on_a_shimmed_select_unfurls_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This path exists because the shim leaves the real ``<select>`` clickable.

    A ``click:`` step therefore lands on the select exactly as before the branch,
    and its ``mousedown`` handler opens the DOM list instead of Chromium's
    unrecordable OS popup.
    """

    path = _write(tmp_path, "click-select.scenario.yaml", CLICK_SCENARIO, FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await run_compile_in_browser(path, browser, SelectReasoner())

        spy.reset()
        out = tmp_path / "click-select.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    assert len(spy.clicks) == 1
    unfurled = spy.clicks[0]
    assert unfurled["open"] == 1
    assert unfurled["labels"] == ["lista", "tabela", "BibTeX"]
