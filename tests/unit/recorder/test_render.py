import asyncio
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.config import ChromeConfig, TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import RenderError, _prime_visuals, run_render
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.video.mux import probe_duration

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg niedostępny"),
]

SCENARIO = textwrap.dedent(
    """\
    config:
      title: Logowanie
      viewport: {width: 640, height: 480}
      tts: {provider: fake, voice: v, lang: pl-PL}
    steps:
      - say: "Witaj, zaraz pokażę logowanie."
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - teach: "kliknij Zaloguj"
    """
)


class MockReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


class TypeReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="type",
            target=RoleTarget(role="textbox", name="E-mail", exact=True),
            input_text="koparka@poczta.wp.pl",
        )


class FakeTts:
    adapter_version = 1
    duration = 0.3

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                str(self.duration),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return self.duration


class SlowTts(FakeTts):
    duration = 0.8


async def test_page_event_primes_visuals_in_window_open_about_blank() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        overlay = Overlay()
        chrome = Chrome(ChromeConfig(enabled=True))
        await overlay.install_context(context)
        await chrome.install_context(context)
        page = await context.new_page()
        await page.set_content("<button onclick=\"window.open('about:blank')\">Open</button>")

        prime_tasks: list[asyncio.Task[None]] = []

        def prime(candidate):
            prime_tasks.append(asyncio.create_task(_prime_visuals(candidate, overlay, chrome)))

        context.on("page", prime)
        async with page.expect_popup() as popup_info:
            await page.get_by_role("button", name="Open").click()
        popup = await popup_info.value
        await asyncio.gather(*prime_tasks)

        assert await popup.locator("[data-guidebot-cursor]").count() == 1
        assert await popup.locator("[data-guidebot-chrome]").count() == 1
        await context.close()
        await browser.close()


def _stream_types(path: Path) -> list[str]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


async def test_render_produces_mp4_with_audio(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1


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
        await run_compile(path, page, MockReasoner())
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
        await run_compile(path, page, TypeReasoner())
        await page.context.close()

        path.write_text(
            scenario.replace("koparka@poczta.wp.pl", "nowy@poczta.wp.pl"),
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="compiled jest nieaktualny"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_fails_when_expected_popup_does_not_open(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        action = compiled.actions[2]
        write_compiled(
            cpath,
            compiled.model_copy(
                update={"actions": [None, None, action.model_copy(update={"opens_popup": True})]}
            ),
        )

        with pytest.raises(RenderError, match="oczekiwany popup"):
            await run_render(
                path,
                tmp_path / "out.mp4",
                FakeTts(),
                tmp_path / "cache",
                browser,
                timeout=0.2,
            )
        await browser.close()


async def test_render_fails_when_popup_closes_during_opening(tmp_path):
    popup_html = tmp_path / "popup.html"
    popup_html.write_text("<h1>Popup</h1>", encoding="utf-8")
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "immediate-close.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        popup_html.write_text("<script>window.close()</script>", encoding="utf-8")
        with pytest.raises(RenderError, match="podczas otwierania"):
            await run_render(
                path,
                tmp_path / "out.mp4",
                FakeTts(),
                tmp_path / "cache",
                browser,
            )
        await browser.close()


async def test_render_fails_on_unexpected_popup(tmp_path):
    html = tmp_path / "popup.html"
    html.write_text(
        "<button onclick=\"window.open('about:blank')\">Zaloguj</button>", encoding="utf-8"
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        action = compiled.actions[1]
        assert action.opens_popup is True
        write_compiled(
            cpath,
            compiled.model_copy(
                update={"actions": [None, action.model_copy(update={"opens_popup": False})]}
            ),
        )
        html.write_text(
            "<button onclick=\"setTimeout(() => window.open('about:blank'), 0)\">Zaloguj</button>",
            encoding="utf-8",
        )

        with pytest.raises(RenderError, match="nieoczekiwany popup"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_does_not_attribute_popup_opened_before_actual_click(tmp_path):
    correct = tmp_path / "correct.html"
    correct.write_text("<h1>Correct popup</h1>", encoding="utf-8")
    early = tmp_path / "early.html"
    early.write_text("<h1>Early popup</h1>", encoding="utf-8")
    html = tmp_path / "main.html"
    button = "<button onclick=\"window.open('correct.html')\">Zaloguj</button>"
    html.write_text(button, encoding="utf-8")
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "pre-click-popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        # The timer fires after narration but while Recorder is moving/settling
        # the synthetic cursor, before Locator.click is actually dispatched.
        html.write_text(
            button + "<script>setTimeout(() => window.open('early.html'), 550)</script>",
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="przed akcją click"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_fails_when_popup_closes_during_narration(tmp_path):
    popup_html = tmp_path / "popup.html"
    popup_html.write_text("<h1>Popup</h1>", encoding="utf-8")
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
          - say: "Popup pozostaje otwarty podczas tej narracji."
        """
    )
    path = tmp_path / "async-close.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        # Keep the compiled target/lifecycle metadata, then simulate runtime drift
        # that closes the popup independently of a scenario action.
        popup_html.write_text(
            "<h1>Popup</h1><script>setTimeout(() => close(), 300)</script>",
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="asynchronicznie"):
            await run_render(path, tmp_path / "out.mp4", SlowTts(), tmp_path / "cache", browser)
        await browser.close()
