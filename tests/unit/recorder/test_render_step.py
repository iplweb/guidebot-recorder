"""``recorder.render`` step wiring: init scripts, open-hold and typing animation.

Split out of the original ``test_render.py``.
"""

import textwrap

from playwright.async_api import async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.desktop import DesktopOverlay
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder import render as render_module
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.selects import Selects
from guidebot_recorder.slide import SlideOverlay

from ._render_helpers import FFMPEG, SCENARIO, FakeTts, MockReasoner

pytestmark = FFMPEG


async def test_run_render_registers_overlay_then_slide_then_chrome_init_scripts(
    tmp_path, monkeypatch
):
    """Locks in the context init-script ordering contract of ``render/stage.py``.

    cursor.js, slide.js and desktop.js rely on reading the real ``window.top``
    to decide whether they are running in the top document or a framed site;
    chrome.js is what shadows ``top`` for frame-bust neutralization. If any of
    them ran after chrome.js, it would read the shadowed ``top`` and
    misidentify its role — desktop.js would mount the whole desktop *inside*
    the framed site. This spies on ``install_context`` (rather than asserting
    on ``window.top`` behavior directly) because modern Chromium already makes
    ``Object.defineProperty(window, "top", ...)`` a no-op for cross-origin
    frames, so a black-box DOM assertion can't distinguish a correct order
    from a swapped one — only the registration order itself can.

    ``selects`` appears in the expected sequence as a record of where it is
    registered, *not* as a constraint on it: the shim's role gating is
    ``isTop && origin === SHELL_ORIGIN``, and chrome.js shadows ``top`` only
    inside framed documents, whose origin is never the shell's, so it reaches
    the same verdict on either side of chrome.js (see the role-gating comment in
    ``selects.js`` and ``Selects.install_context``). What this test does assert
    about the shim is the render half of the spec §1 installation table: the
    render context is one of the three that drive pages, so the widget must be
    installed on it at all.
    """
    path = tmp_path / "chrome.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Chrome
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              chrome: {enabled: true}
            steps:
              - say: "Witaj."
            """
        ),
        encoding="utf-8",
    )

    order: list[str] = []
    original_overlay_install = Overlay.install_context
    original_slide_install = SlideOverlay.install_context
    original_desktop_install = DesktopOverlay.install_context
    original_selects_install = Selects.install_context
    original_chrome_install = Chrome.install_context

    async def spy_overlay_install(self, context):
        order.append("overlay")
        return await original_overlay_install(self, context)

    async def spy_slide_install(self, context):
        order.append("slide")
        return await original_slide_install(self, context)

    async def spy_desktop_install(self, context):
        order.append("desktop")
        return await original_desktop_install(self, context)

    async def spy_selects_install(self, context):
        order.append("selects")
        return await original_selects_install(self, context)

    async def spy_chrome_install(self, context):
        order.append("chrome")
        return await original_chrome_install(self, context)

    monkeypatch.setattr(Overlay, "install_context", spy_overlay_install)
    monkeypatch.setattr(SlideOverlay, "install_context", spy_slide_install)
    monkeypatch.setattr(DesktopOverlay, "install_context", spy_desktop_install)
    monkeypatch.setattr(Selects, "install_context", spy_selects_install)
    monkeypatch.setattr(Chrome, "install_context", spy_chrome_install)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert order == ["overlay", "slide", "desktop", "selects", "chrome"]


async def test_render_passes_the_configured_open_hold_to_the_recorder(tmp_path, monkeypatch):
    """``config.selects.openHoldMs`` must reach the beat-2 pause, or it is inert.

    The kwarg's own default happens to equal ``SelectsConfig``'s, so a render
    that never forwards the configured value still produces a plausible film —
    the setting simply does nothing, silently.
    """
    path = tmp_path / "hold.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Hold
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              selects: {openHoldMs: 123}
            steps:
              - say: "Witaj."
            """
        ),
        encoding="utf-8",
    )

    holds: list[float | None] = []
    original_recorder = render_module.loop.Recorder

    class SpyRecorder(original_recorder):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            holds.append(kwargs.get("open_hold_ms"))
            super().__init__(*args, **kwargs)

    # `Recorder` is constructed in two submodules — the render loop and the
    # post-popup-close funnel — so replacing it takes both lines.
    monkeypatch.setattr(render_module.loop, "Recorder", SpyRecorder)
    monkeypatch.setattr(render_module.visuals, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert holds and set(holds) == {123}


async def test_render_wires_viewport_and_typing_animation(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    scenario_with_typing = textwrap.dedent(
        """\
        config:
          title: Logowanie
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
          typing: {animate: true, speed: 55}
        steps:
          - say: "Witaj, zaraz pokażę logowanie."
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "typing.scenario.yaml"
    path.write_text(scenario_with_typing, encoding="utf-8")

    overlay_viewports: list = []
    recorder_kwargs: list = []

    class SpyOverlay(R.stage.Overlay):
        def __init__(self, cursor=None, viewport=None):
            overlay_viewports.append(viewport)
            super().__init__(cursor, viewport)

    class SpyRecorder(R.loop.Recorder):
        def __init__(self, *a, **k):
            recorder_kwargs.append(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(R.stage, "Overlay", SpyOverlay)
    # `Recorder` is constructed in two submodules — the render loop and the
    # post-popup-close funnel — so replacing it takes both lines.
    monkeypatch.setattr(R.loop, "Recorder", SpyRecorder)
    monkeypatch.setattr(R.visuals, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert overlay_viewports[0] is not None
    assert overlay_viewports[0].width == 640
    assert any(k.get("type_delay_ms") == 55 for k in recorder_kwargs)


async def test_render_respects_typing_animate_false(tmp_path, monkeypatch):
    # Typing now animates by default (see test_config defaults); this guards the
    # explicit opt-out: `typing.animate: false` must leave the Recorder without a
    # per-character delay so fields fill instantly.
    import guidebot_recorder.recorder.render as R

    scenario = SCENARIO.replace(
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n",
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n  typing: {animate: false}\n",
    )
    path = tmp_path / "no-typing.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    recorder_kwargs: list = []

    class SpyRecorder(R.loop.Recorder):
        def __init__(self, *a, **k):
            recorder_kwargs.append(k)
            super().__init__(*a, **k)

    # `Recorder` is constructed in two submodules — the render loop and the
    # post-popup-close funnel — so replacing it takes both lines.
    monkeypatch.setattr(R.loop, "Recorder", SpyRecorder)
    monkeypatch.setattr(R.visuals, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert recorder_kwargs
    assert all(k.get("type_delay_ms") is None for k in recorder_kwargs)
