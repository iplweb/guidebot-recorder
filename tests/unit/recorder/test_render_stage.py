"""``recorder.render.stage``: the slide card and the intro bootstrap frame.

Split out of the original ``test_render.py``.
"""

import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import RenderError, run_render
from guidebot_recorder.video.mux.probe import probe_duration

from ._render_helpers import FFMPEG, SCENARIO, FakeTts, MockReasoner

pytestmark = FFMPEG


# --- Slide cards + auto-intro (Task 5.3) -------------------------------------

SLIDE_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Prezentacja
      viewport: {width: 640, height: 480}
      tts: {provider: fake, voice: v, lang: pl-PL}
    steps:
      - slide: {title: "Witaj w GuideBot", hold: 0.05}
      - say: "To jest wprowadzenie."
    """
)


async def test_slide_step_paints_card_and_hides_layers(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide.scenario.yaml"
    path.write_text(SLIDE_SCENARIO, encoding="utf-8")

    slide_events: list[tuple[str, dict]] = []

    class SpySlide(R.stage.SlideOverlay):
        async def show(self, page, card):
            await super().show(page, card)
            slide_events.append(("show", dict(card)))

        async def ensure(self, page, card):
            await super().ensure(page, card)
            dom_count = await page.locator("[data-guidebot-slide]").count()
            cursor_display = await page.evaluate(
                "() => document.querySelector('[data-guidebot-cursor]')?.style.display"
            )
            slide_events.append(
                (
                    "ensure",
                    {"card": dict(card), "dom_count": dom_count, "cursor_display": cursor_display},
                )
            )

    monkeypatch.setattr(R.stage, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    show_events = [payload for kind, payload in slide_events if kind == "show"]
    ensure_events = [payload for kind, payload in slide_events if kind == "ensure"]
    assert show_events, "the slide step never called SlideOverlay.show"
    assert show_events[0] == {
        "title": "Witaj w GuideBot",
        "subtitle": None,
        "notes": None,
    }
    assert ensure_events, "the say step never re-asserted the card via _ensure_card"
    # While the `say` narrates, the card must be mounted and the cursor hidden.
    assert ensure_events[0]["dom_count"] == 1
    assert ensure_events[0]["cursor_display"] == "none"
    assert out.exists()
    assert probe_duration(out) > 0


async def test_teach_or_navigate_after_slide_dismisses_card(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-navigate.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.05}
              - navigate: "data:text/html,<p>Po slajdzie</p>"
            """
        ),
        encoding="utf-8",
    )

    slide_hide_calls = 0
    overlay_show_calls = 0
    dom_state_before_navigate: list[int] = []

    class SpySlide(R.stage.SlideOverlay):
        async def hide(self, page):
            nonlocal slide_hide_calls
            slide_hide_calls += 1
            await super().hide(page)

    class SpyOverlay(R.stage.Overlay):
        async def show(self, page):
            nonlocal overlay_show_calls
            overlay_show_calls += 1
            await super().show(page)

    class SpyRecorder(R.loop.Recorder):
        async def navigate(self, url):
            dom_state_before_navigate.append(
                await self.page.locator("[data-guidebot-slide]").count()
            )
            await super().navigate(url)

    monkeypatch.setattr(R.stage, "SlideOverlay", SpySlide)
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

    assert slide_hide_calls >= 1, "the navigate step never dismissed the card"
    assert overlay_show_calls >= 1, "the navigate step never restored the cursor"
    assert dom_state_before_navigate == [0], "the card was still mounted when navigate ran"
    assert out.exists()


async def test_navigation_destroying_card_mid_say_fails_loud(tmp_path, monkeypatch):
    """GAP 1: the card is destroyed by a navigation DURING the say's narration
    wait, and the say is the LAST step. The pre-narration `_ensure_card` check
    already passed (card was still alive then); only the post-narration re-check
    can catch this. Without it the video narrates over the wrong page and render
    completes silently — so the render MUST raise RenderError instead."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-fail.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja nad znikającą kartą."
            """
        ),
        encoding="utf-8",
    )

    # The card is "destroyed" (token goes falsy, as a fresh navigated document
    # would report) only AFTER the narration wait completes — i.e. the
    # destruction happens DURING the wait, never before it. The pre-narration
    # check therefore sees a live card; only a post-wait check sees the loss.
    destroyed = {"value": False}

    class MidWaitDestroySlide(R.stage.SlideOverlay):
        async def token(self, page):
            if destroyed["value"]:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R.stage, "SlideOverlay", MidWaitDestroySlide)

    original_wait = R.narration._pace_narration

    async def destroy_during_wait(segments, **kwargs):
        await original_wait(segments, **kwargs)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R.narration, "_pace_narration", destroy_during_wait)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert destroyed["value"], "the narration-wait wrapper never ran"
    assert not out.exists()


async def test_slide_after_card_destroyed_during_say_fails_loud(tmp_path, monkeypatch):
    """GAP 2 (shape): a `[slide, say, slide]` scenario where the middle say's
    card is destroyed during its narration wait must also raise RenderError —
    the shape must never complete silently.

    The spy models reality: `token` is falsy while destroyed, but a real
    `show()` (the trailing slide's repaint) restores a truthy token. Without the
    post-narration re-check this shape completes SILENTLY, because the trailing
    slide repaints a fresh card over the wrong page, restoring a valid token, so
    that slide's own hold-loop check then passes."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-say-slide.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja nad znikającą kartą."
              - slide: {title: "Krok 2", hold: 0.0}
            """
        ),
        encoding="utf-8",
    )

    destroyed = {"value": False}

    class GhostNavSlide(R.stage.SlideOverlay):
        async def show(self, page, card):
            await super().show(page, card)
            destroyed["value"] = False  # a repaint restores a truthy token

        async def token(self, page):
            if destroyed["value"]:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R.stage, "SlideOverlay", GhostNavSlide)

    original_wait = R.narration._pace_narration

    async def destroy_during_wait(segments, **kwargs):
        await original_wait(segments, **kwargs)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R.narration, "_pace_narration", destroy_during_wait)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert not out.exists()


async def test_slide_dismiss_fails_loud_when_card_destroyed_after_say(tmp_path, monkeypatch):
    """GAP 2 (isolates the slide-dismiss token assert): a navigation lands
    AFTER the say has fully completed (so its post-narration re-check already
    passed) but BEFORE the following slide dismisses the card. This reproduces
    the realistic race the slide-dismiss `_assert_card_alive` guards: without
    it, the next slide silently repaints a fresh card (restoring a truthy
    token) over the navigated page, and the render succeeds silently.

    The spy models reality: `token` is falsy while the ghost-navigation is in
    effect, but a real `show()` (the next slide's repaint) restores a truthy
    token — exactly what a fresh document's first `show()` would do."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-say-slide-race.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja, po której następuje nawigacja."
              - slide: {title: "Krok 2", hold: 0.0}
            """
        ),
        encoding="utf-8",
    )

    slide_ref: dict = {}

    class GhostNavSlide(R.stage.SlideOverlay):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._ghost = False
            slide_ref["slide"] = self

        async def show(self, page, card):
            await super().show(page, card)
            # A real show() in a fresh document bumps the token to truthy; model
            # the repaint restoring a valid token so the bug (silent repaint over
            # the wrong page) is faithfully reproduced.
            self._ghost = False

        async def token(self, page):
            if self._ghost:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R.stage, "SlideOverlay", GhostNavSlide)

    original_render_step = R._step._render_step

    async def render_step_spy(*args, **kwargs):
        # args[6] is `kind` (see _render_step's signature). A navigation lands
        # the instant the say step finishes — after its post-narration re-check.
        kind = args[6]
        result = await original_render_step(*args, **kwargs)
        if kind == "say":
            slide_ref["slide"]._ghost = True
        return result

    monkeypatch.setattr(R._step, "_render_step", render_step_spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert not out.exists()


async def test_intro_enabled_replaces_bootstrap(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "intro-on.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Logowanie
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              intro: {enabled: true, subtitle: "Poznaj system", notes: "Zaczynamy"}
            steps:
              - say: "Witaj, zaraz pokażę logowanie."
              - navigate: "data:text/html,<button>Zaloguj</button>"
              - teach: "kliknij Zaloguj"
            """
        ),
        encoding="utf-8",
    )

    show_calls: list[dict] = []

    class SpySlide(R.stage.SlideOverlay):
        async def show(self, page, card):
            show_calls.append(dict(card))
            await super().show(page, card)

    monkeypatch.setattr(R.stage, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert show_calls, "intro.enabled=True never painted a card at bootstrap"
    # The FIRST show() call is the bootstrap intro card (no `slide` step exists
    # in this scenario, so there is no other candidate call).
    assert show_calls[0] == {
        "title": "Logowanie",
        "subtitle": "Poznaj system",
        "notes": "Zaczynamy",
    }
    assert out.exists()
    assert probe_duration(out) > 0


async def test_intro_disabled_bootstrap_unchanged(tmp_path, monkeypatch):
    """The critical back-compat guarantee: `intro.enabled=False` never paints a
    card and never calls SlideOverlay.show — bootstrap is byte-identical to
    pre-Task-5.3 behavior."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "intro-off.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")  # intro defaults to disabled

    show_calls: list[dict] = []

    class SpySlide(R.stage.SlideOverlay):
        async def show(self, page, card):
            show_calls.append(dict(card))
            await super().show(page, card)

    monkeypatch.setattr(R.stage, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert show_calls == []
    assert out.exists()
    assert probe_duration(out) > 0
