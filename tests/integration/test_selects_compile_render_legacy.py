"""E2E: a pre-shim ``*.compiled.yaml`` still renders *with* the shim installed.

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

from guidebot_recorder.models.config import config_hash
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import install_selects

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


# No ``selects:`` block at all: this is what a scenario looked like before the
# branch, and it is what the back-compat claim is about.
LEGACY_SCENARIO = """\
config:
  title: Zgodność wstecz
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Wybieram format."
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX"}}
  - select: {{from: "lista województw", option: "Mazowieckie"}}
"""


# --- back-compatibility -----------------------------------------------------


async def test_artifact_compiled_without_the_shim_still_renders_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most expensive claim on this branch, pinned end to end.

    A ``*.compiled.yaml`` produced *before* the feature must still render *with*
    it, with no recompile. The claim rests on §3: the shim never re-parents the
    ``<select>``, so the frozen ``ancestry_digest`` — a hash of the whole composed
    ancestor chain — stays valid and ``reuse_is_valid`` keeps passing. A wrapper
    design would fail here on every cached action under the select.

    The artifact is built the way a pre-feature compile built one: a bare context
    with no init script at all (``selects=None``), from a scenario with no
    ``selects:`` block, so ``config_hash`` is the pre-feature hash too.
    """

    path = _write(tmp_path, "legacy.scenario.yaml", LEGACY_SCENARIO, FIXTURE)
    url = FIXTURE.resolve().as_uri()
    scenario = load_scenario(path)
    spy = SelectSpy()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        # --- compile exactly as the pre-feature code did: no shim anywhere ---
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await run_compile(path, page, SelectReasoner(), selects=None)
        shimless = await page.evaluate(
            "() => ({api: !!window.__guidebot_selects,"
            " overlays: document.querySelectorAll("
            "'[data-guidebot-select-button],[data-guidebot-select-list]').length})"
        )
        assert shimless == {"api": False, "overlays": 0}
        await context.close()

        artifact = compiled_path(path)
        frozen_bytes = artifact.read_bytes()
        compiled = load_compiled(artifact)
        selects = [action for action in compiled.actions if action is not None]
        assert [action.action for action in selects] == ["select", "select"]
        # The default mode stays out of the projection, so the hash is unchanged.
        assert {action.fingerprint.config_hash for action in selects} == {
            config_hash(scenario.config)
        }

        # --- the identities survive the shim, checked directly ---
        shimmed_context = await browser.new_context(viewport={"width": 800, "height": 600})
        controller = await install_selects(shimmed_context, scenario.config)
        assert controller is not None
        shimmed_page = await shimmed_context.new_page()
        await shimmed_page.goto(url)
        await controller.wait_ready(shimmed_page)
        shim_lists = await shimmed_page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-list]').length"
        )
        assert shim_lists == 1  # the raw select really is shimmed on this page
        for action in selects:
            assert await reuse_is_valid(shimmed_page, action), action.identity
        await shimmed_context.close()

        # --- render the pre-feature artifact, with the shim installed ---
        spy.install(monkeypatch)
        out = tmp_path / "legacy.mp4"
        # A regression here surfaces as RenderError "niezgodna tożsamość —
        # uruchom `compile --force`", which is precisely the forced recompile §5
        # promises never to happen.
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    # Nothing was re-resolved: the sidecar on disk is byte-identical.
    assert compiled_path(path).read_bytes() == frozen_bytes
    assert [record["selected"] for record in spy.selects] == [["BibTeX"], ["Mazowieckie"]]
    # And the pre-feature artifact drove the *new* choreography: a real list.
    for record in spy.selects:
        assert [event["kind"] for event in record["events"]] == ["control", "option"]
        opened = record["events"][1]
        assert opened["visible"] is True
        assert opened["attached"] is True
