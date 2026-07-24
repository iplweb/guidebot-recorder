"""E2E: the shimmed drop-down inside a popup window, through compile → render.

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
    POPUP_FIXTURE,
    PYTESTMARK,
    FakeTts,
    SelectReasoner,
    SelectSpy,
    _assert_playable,
    _write,
)

pytestmark = list(PYTESTMARK)


POPUP_SCENARIO = """\
config:
  title: Lista w popupie
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - click: "Otwórz okno zamówienia"
  - select: {{from: "lista sposobów dostawy", option: "Kurier"}}
  - wait: 0.4
"""


# --- a select inside a popup window -----------------------------------------


async def test_select_inside_a_popup_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The init script is context-level, so a popup gets the widget too.

    Worth its own test: in a popup the cursor and the shim list share a single
    document, so the list's z-index has to sit below the cursor's yet above the
    page — in the main window the cursor lives in the shell, above the iframe.
    """

    path = _write(tmp_path, "popup-select.scenario.yaml", POPUP_SCENARIO, POPUP_FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await run_compile_in_browser(path, browser, SelectReasoner())

        compiled = load_compiled(compiled_path(path))
        assert compiled.actions[2] is not None and compiled.actions[2].opens_popup is True

        spy.reset()
        out = tmp_path / "popup-select.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    assert len(spy.selects) == 1
    record = spy.selects[0]
    assert record["selected"] == ["Kurier"]
    assert record["shims"] == {"dostawa": True}
    assert [event["kind"] for event in record["events"]] == ["control", "option"]
    opened = record["events"][1]
    assert opened["label"] == "Kurier"
    assert opened["attached"] is True
    assert opened["visible"] is True
    assert opened["labels"] == ["Odbiór osobisty", "Paczkomat", "Kurier"]
