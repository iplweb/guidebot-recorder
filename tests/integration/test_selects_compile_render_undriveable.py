"""E2E: an undriveable ``<select>`` stops the run instead of setting the value.

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
through that import, so they are re-declared below from ``PYTESTMARK``. The
``_the_select_itself`` stand-in below is local: it exists only to freeze the
one artifact this test needs and is used nowhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import guidebot_recorder.resolver.validate as validate_module
from guidebot_recorder.recorder.compile import run_compile, run_compile_in_browser
from guidebot_recorder.recorder.render import RenderError, run_render

from ._selects_e2e import (
    PYTESTMARK,
    UNDRIVEABLE_FIXTURE,
    FakeTts,
    SelectReasoner,
    SelectSpy,
    _write,
)

pytestmark = list(PYTESTMARK)


UNDRIVEABLE_SCENARIO = """\
config:
  title: Lista bez kontrolki
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Wybieram województwo."
  - navigate: "{url}"
  - select: {{from: "lista województw", option: "Mazowieckie"}}
"""


# --- the undriveable widget -------------------------------------------------


async def _the_select_itself(locator):
    """``user_visible_control`` as it behaved before the predicate was shared.

    Playwright's ``is_visible()`` is true for select2's 1x1-clipped original, so
    the old first step handed the ``<select>`` straight back and validation
    passed. Standing in for it here is what produces the artifact this test
    needs: one an older Guidebot could really have written.
    """

    return await locator.element_handle()


async def test_undriveable_widget_fails_loudly_instead_of_setting_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden ``<select>`` with no visible stand-in must stop the run.

    There is deliberately no fallback to ``select_option()``: it would restore
    exactly the invisible value change the branch exists to remove, and would do
    it unobservably.

    Compile stops it, and *where* it stops is the point. The 1x1-clipped original
    is hidden to the shared predicate, so validation refuses the page and
    ``Recorder.select`` is never reached at all — a step that never runs cannot
    touch the value. Validation used to accept it (Playwright calls a 1x1 box
    visible) and leave the rejecting to the drivability probe one layer down; the
    two compile-time checks disagreeing about the same page is the drift the
    shared predicate removes.

    The second half covers the artifact frozen by an older, more permissive
    validator. Render re-validates a frozen target before reusing it, so this one
    is refused there — the run stops rather than degrading to a silent
    ``select_option``, which is the property that matters. It stops with the
    generic "recompile" verdict of the reuse check rather than the choreography's
    own wording, because it never gets as far as the choreography; recompiling is
    what surfaces the full diagnosis, and the first half is that recompile.
    """

    path = _write(tmp_path, "orphan.scenario.yaml", UNDRIVEABLE_SCENARIO, UNDRIVEABLE_FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        with pytest.raises(RuntimeError) as compile_error:
            await run_compile_in_browser(path, browser, SelectReasoner())
        message = str(compile_error.value)
        assert "nie znaleziono widocznej kontrolki" in message
        assert "lista województw" in message  # names the step's own instruction
        # ...and, since the fix is an edit to the scenario, the line to edit.
        assert f"{path}:" in message
        # Nothing was ever driven, so nothing could have been quietly set.
        assert spy.selects == []

        # Freeze the artifact the way a Guidebot that still trusted Playwright's
        # `is_visible()` would have frozen it, then render that.
        monkeypatch.setattr(validate_module, "user_visible_control", _the_select_itself)
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await run_compile(path, page, SelectReasoner(), selects=None)
        await context.close()
        monkeypatch.undo()

        spy.reset()
        spy.install(monkeypatch)
        with pytest.raises(RenderError) as render_error:
            await run_render(path, tmp_path / "orphan.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    # 1-based and located, like every other step message since the YAML step
    # diagnostics landed: `say` + `navigate` + `select` → the select is 3rd.
    message = str(render_error.value)
    assert f"krok 3/3 — {path}:" in message
    assert 'from: "lista województw"' in message  # the fragment to edit
    assert "compile" in message  # points at the run that diagnoses it
    # Again: refused before anything was driven, so the value was never set.
    assert spy.selects == []
