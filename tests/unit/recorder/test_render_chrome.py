from __future__ import annotations

from dataclasses import dataclass

from guidebot_recorder.models.config import ChromeConfig, Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Step
from guidebot_recorder.recorder.render import _expect_chrome, _render_step


def test_expect_chrome_tracks_the_legacy_bar() -> None:
    chrome = object()  # any non-None controller stand-in

    # Chrome disabled → the legacy bar never exists.
    assert _expect_chrome(None, False) is False
    assert _expect_chrome(None, True) is False

    # Chrome enabled, non-floating → legacy bar expected (context-wide).
    assert _expect_chrome(chrome, False) is True

    # Floating (bare) popups suppress the legacy bar everywhere — including the
    # main window's about:blank warm-up; its real chrome is the shell.
    assert _expect_chrome(chrome, True) is False


@dataclass
class FakePage:
    url: str = "about:blank"

    async def evaluate(self, script, arg=None):
        if isinstance(arg, bool):
            return {"cursor": False, "chrome": False}
        return None


class FakeOverlay:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.pos = (0.0, 0.0)

    async def ensure(self, page: FakePage) -> None:
        self.events.append(("overlay.ensure", page.url))


class FakeChrome:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    async def ensure(self, page: FakePage) -> None:
        self.events.append(("chrome.ensure", page.url))

    async def set_url(self, page: FakePage, url: str, *, animate: bool = True) -> None:
        self.events.append(("chrome.set_url", url, animate))


class FakeRecorder:
    def __init__(self, page: FakePage, events: list[tuple], final_url: str) -> None:
        self.page = page
        self.events = events
        self.final_url = final_url

    async def navigate(self, url: str) -> None:
        self.events.append(("recorder.navigate", url))
        self.page.url = self.final_url


def _scenario(step: Step, *, type_on_navigate: bool = True, show_url: bool = True) -> Scenario:
    return Scenario(
        config=Config(
            title="t",
            viewport=Viewport(width=800, height=600),
            tts=TtsConfig(provider="fake", voice="v", lang="pl"),
            baseUrl="https://example.com/base/",
            chrome=ChromeConfig(
                enabled=True,
                showUrl=show_url,
                typeOnNavigate=type_on_navigate,
            ),
        ),
        steps=[step],
    )


async def _run(step: Step, *, type_on_navigate: bool = True, show_url: bool = True):
    events: list[tuple] = []
    page = FakePage()
    overlay = FakeOverlay(events)
    chrome = FakeChrome(events)
    recorder = FakeRecorder(page, events, "https://redirected.example/final")
    scenario = _scenario(
        step,
        type_on_navigate=type_on_navigate,
        show_url=show_url,
    )

    await _render_step(
        page,
        recorder,
        overlay,
        chrome,
        scenario,
        step,
        "navigate",
        0,
        None,
        0.0,
        {},
    )
    return events


async def test_navigate_types_resolved_url_before_goto_and_reensures_afterward() -> None:
    events = await _run(Step(navigate={"url": "login", "type": True}))

    assert events == [
        ("chrome.ensure", "about:blank"),
        ("overlay.ensure", "about:blank"),
        ("chrome.set_url", "https://example.com/base/login", True),
        ("recorder.navigate", "https://example.com/base/login"),
        ("chrome.ensure", "https://redirected.example/final"),
        ("overlay.ensure", "https://redirected.example/final"),
    ]


async def test_navigate_without_typing_sets_redirected_url_after_goto() -> None:
    events = await _run(Step(navigate={"url": "login", "type": False}))

    assert events == [
        ("chrome.ensure", "about:blank"),
        ("overlay.ensure", "about:blank"),
        ("recorder.navigate", "https://example.com/base/login"),
        ("chrome.set_url", "https://redirected.example/final", False),
        ("chrome.ensure", "https://redirected.example/final"),
        ("overlay.ensure", "https://redirected.example/final"),
    ]


async def test_string_navigate_inherits_type_on_navigate() -> None:
    events = await _run(Step(navigate="login"), type_on_navigate=False)

    assert ("chrome.set_url", "https://redirected.example/final", False) in events
    assert ("chrome.set_url", "https://example.com/base/login", True) not in events


async def test_hidden_url_skips_both_animated_and_instant_updates() -> None:
    events = await _run(Step(navigate={"url": "login", "type": True}), show_url=False)

    assert not any(event[0] == "chrome.set_url" for event in events)
    assert ("recorder.navigate", "https://example.com/base/login") in events


async def test_bare_popup_step_ensures_cursor_but_not_chrome() -> None:
    """A bare (floating) popup must restore the cursor without demanding chrome."""

    events: list[tuple] = []
    page = FakePage()
    overlay = FakeOverlay(events)
    chrome = FakeChrome(events)
    recorder = FakeRecorder(page, events, "unused")
    step = Step(say="hi")
    scenario = _scenario(step)

    await _render_step(
        page,
        recorder,
        overlay,
        chrome,
        scenario,
        step,
        "say",
        0,
        None,
        0.0,
        {},
        expect_chrome=False,
    )

    assert ("overlay.ensure", "about:blank") in events
    assert not any(event[0] == "chrome.ensure" for event in events)


async def test_non_bare_step_ensures_chrome() -> None:
    """The default (non-bare) path still repairs the chrome bar on every step."""

    events: list[tuple] = []
    page = FakePage()
    overlay = FakeOverlay(events)
    chrome = FakeChrome(events)
    recorder = FakeRecorder(page, events, "unused")
    step = Step(say="hi")
    scenario = _scenario(step)

    await _render_step(page, recorder, overlay, chrome, scenario, step, "say", 0, None, 0.0, {})

    assert ("chrome.ensure", "about:blank") in events
    assert ("overlay.ensure", "about:blank") in events
