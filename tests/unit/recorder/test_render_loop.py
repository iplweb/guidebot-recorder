"""``recorder.render.loop``: the popup lifecycle inside the render loop.

Split out of the original ``test_render.py``: the compositor frame check plus
the open/close/attribution failure paths.
"""

import asyncio
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.config import ChromeConfig, CursorConfig
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import RenderError, _prime_visuals, run_render
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.video.mux import compose_popup_video

from ._render_helpers import FFMPEG, SCENARIO, FakeTts, MockReasoner

pytestmark = FFMPEG


async def test_compositor_starts_popup_at_verified_visual_frame(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:duration=2:size=640x480:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(main),
        ],
        check=True,
        capture_output=True,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 640, "height": 480},
            record_video_dir=str(tmp_path),
            record_video_size={"width": 640, "height": 480},
        )
        overlay = Overlay(
            CursorConfig(
                width=72,
                height=96,
                color="#ff00ff",
                outline="#ff00ff",
                glow="transparent",
            )
        )
        overlay.pos = (300.0, 200.0)
        chrome = Chrome(ChromeConfig(enabled=True, showUrl=False, barColor="#00ff00"))
        await overlay.install_context(context)
        await chrome.install_context(context)
        page = await context.new_page()
        await page.set_content(
            """<button onclick="
                const child = window.open('about:blank');
                setTimeout(() => {
                    child.document.open();
                    child.document.write('<h1>Final popup</h1>');
                    child.document.close();
                }, 75);
            ">Open</button>"""
        )

        prime_tasks: list[tuple[float, asyncio.Task[float | None]]] = []

        def prime(candidate):
            prime_tasks.append(
                (time.monotonic(), asyncio.create_task(_prime_visuals(candidate, overlay, chrome)))
            )

        context.on("page", prime)
        for run_index in range(5):
            task_count = len(prime_tasks)
            async with page.expect_popup() as popup_info:
                await page.get_by_role("button", name="Open").click()
            popup = await popup_info.value
            assert len(prime_tasks) == task_count + 1
            opened_at, prime_task = prime_tasks[-1]
            visual_ready_at = await prime_task
            assert visual_ready_at is not None

            assert await popup.get_by_role("heading", name="Final popup").count() == 1
            assert await popup.locator("[data-guidebot-cursor]").count() == 1
            assert await popup.locator("[data-guidebot-chrome]").count() == 1
            await popup.wait_for_timeout(300)
            video = popup.video
            assert video is not None
            await popup.close()
            closed_at = time.monotonic()
            webm = Path(await video.path())
            composite = tmp_path / f"composite-{run_index}.mp4"
            timeline_opened_at = 0.2
            compose_popup_video(
                main,
                webm,
                composite,
                opened_at=timeline_opened_at,
                closed_at=timeline_opened_at + (closed_at - opened_at),
                visual_ready_delay=visual_ready_at - opened_at,
            )
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(composite),
                    "-t",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            frame_size = 640 * 480 * 3
            assert len(raw) >= frame_size
            assert len(raw) % frame_size == 0

            def pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
                offset = (y * 640 + x) * 3
                return tuple(frame[offset : offset + 3])

            first_popup_frame = None
            for offset in range(0, len(raw), frame_size):
                frame = raw[offset : offset + frame_size]
                red, green, blue = pixel(frame, 620, 400)
                if red > 180 and green > 180 and blue > 180:
                    first_popup_frame = frame
                    break
            assert first_popup_frame is not None

            red, green, blue = pixel(first_popup_frame, 620, 20)
            assert green > 150 and red < 100 and blue < 100
            cursor_pixels = (
                pixel(first_popup_frame, x, y) for y in range(200, 296) for x in range(300, 372)
            )
            assert any(
                red > 140 and green < 120 and blue > 140 for red, green, blue in cursor_pixels
            )

        await context.close()
        await browser.close()


async def test_render_fails_when_expected_popup_does_not_open(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
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
        await run_compile(path, page, MockReasoner(), selects=None)
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
        await run_compile(path, page, MockReasoner(), selects=None)
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


async def test_render_does_not_attribute_popup_opened_before_actual_click(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    correct = tmp_path / "correct.html"
    correct.write_text("<h1>Correct popup</h1>", encoding="utf-8")
    early = tmp_path / "early.html"
    early.write_text("<h1>Early popup</h1>", encoding="utf-8")
    html = tmp_path / "main.html"
    button = "<button onclick=\"window.open('correct.html')\">Zaloguj</button>"
    html.write_text(button, encoding="utf-8")
    # ``settleMs`` is shrunk to keep the select shim's readiness barrier (taken
    # before the step is resolved) from eating the 550 ms window this test aims
    # the timer at; the barrier is orthogonal to popup attribution.
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
          selects: {{settleMs: 20}}
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
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        # The site opens a window of its own inside the click step, but strictly
        # before Locator.click is dispatched — the render must not credit that
        # window to the click. Hanging it on the recorder's click pins the order;
        # the timer this replaces merely aimed at the gap between the narration
        # and the dispatch, and missing that gap loses the test either way (too
        # early raises a different error, too late and the render is right to
        # attribute the window to the click).
        early_uri = early.resolve().as_uri()

        class EarlyWindowRecorder(R.loop.Recorder):
            async def click(self, target, *, before_click=None):
                # Awaiting the context's own page event is what makes this
                # deterministic: the render has *observed* the window by the time
                # the click runs, rather than merely having been told to expect one.
                async with self.page.context.expect_page() as early_window:
                    await self.page.evaluate("url => window.open(url)", early_uri)
                await early_window.value
                await super().click(target, before_click=before_click)

        # `Recorder` is constructed in two submodules — the render loop and the
        # post-popup-close funnel — so replacing it takes both lines.
        monkeypatch.setattr(R.loop, "Recorder", EarlyWindowRecorder)
        monkeypatch.setattr(R.visuals, "Recorder", EarlyWindowRecorder)

        with pytest.raises(RenderError, match="przed akcją click"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_fails_when_popup_closes_during_narration(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

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
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        # Keep the compiled target/lifecycle metadata, then simulate runtime drift
        # that closes the popup independently of a scenario action. The drift is
        # fired *from the narration* rather than from a timer in the page: a
        # wall-clock delay only bets on which phase the popup dies in, and the
        # render answers that death differently — and just as correctly — in
        # every phase it can land in (during the open, during the visual mount,
        # during the narration). The bet is what made this test flaky on CI; the
        # phase this test is about is the one it now pins.
        real_pace_narration = R.narration._pace_narration

        async def close_popup_during_narration(*args, **kwargs):
            for context in browser.contexts:
                for open_page in list(context.pages):
                    if open_page.url.endswith("popup.html"):
                        await open_page.close()
            return await real_pace_narration(*args, **kwargs)

        monkeypatch.setattr(R.narration, "_pace_narration", close_popup_during_narration)

        with pytest.raises(RenderError, match="asynchronicznie podczas narracji"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()
