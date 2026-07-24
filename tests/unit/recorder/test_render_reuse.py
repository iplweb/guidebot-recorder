"""``recorder.render.reuse``/``plan`` preflight guards: sidecar, cache and version checks.

Split out of the original ``test_render.py``.
"""

import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import RenderError, run_render
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled

from ._render_helpers import FFMPEG, SCENARIO, FakeTts, MockReasoner, TypeReasoner

pytestmark = FFMPEG


async def test_render_rejects_sidecar_from_other_scenario_before_tts_or_browser(tmp_path):
    path = tmp_path / "narration.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Narracja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: pl, lang: pl-PL}
            steps:
              - say: "Witaj."
            """
        ),
        encoding="utf-8",
    )
    write_compiled(
        compiled_path(path),
        CompiledScenario(source="english.scenario.yaml", actions=[None]),
    )

    with pytest.raises(RenderError, match="innego scenariusza"):
        await run_render(
            path,
            tmp_path / "out.mp4",
            FakeTts(),
            tmp_path / "cache",
            object(),  # type: ignore[arg-type] -- provenance fails before browser use
        )

    assert not (tmp_path / "cache").exists()


async def test_render_without_cache_raises(tmp_path):
    scenario = textwrap.dedent(
        """\
        config:
          title: t
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "s.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        with pytest.raises(RenderError):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_rejects_old_compiler_version(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        write_compiled(
            cpath,
            compiled.model_copy(update={"compiler_version": COMPILER_VERSION - 1}),
        )

        with pytest.raises(RenderError, match="starszą wersję"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_rejects_teach_text_changed_after_compile(tmp_path):
    scenario = textwrap.dedent(
        """\
        config:
          title: t
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<input aria-label='E-mail'>"
          - teach: "wpisz koparka@poczta.wp.pl w pole E-mail"
        """
    )
    path = tmp_path / "type.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, TypeReasoner(), selects=None)
        await page.context.close()

        path.write_text(
            scenario.replace("koparka@poczta.wp.pl", "nowy@poczta.wp.pl"),
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="compiled jest nieaktualny"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()
