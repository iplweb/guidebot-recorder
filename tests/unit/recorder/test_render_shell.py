"""Unit tests for the shell (main-window) render path: config matrix, geometry,
and the frame-sourced address bar — all with fakes, no browser."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from guidebot_recorder.chrome import SHELL_URL
from guidebot_recorder.models.config import ChromeConfig, Config, TtsConfig, Viewport, site_viewport
from guidebot_recorder.models.scenario import Scenario, Step
from guidebot_recorder.recorder.render import _render_step, navigate_pill_mode

# --- Config matrix (navigate_pill_mode) ------------------------------------


@pytest.mark.parametrize(
    ("interact", "type_on_navigate", "override", "expected"),
    [
        (True, True, None, "choreograph"),
        (False, True, None, "type"),
        (True, False, None, "instant"),
        (False, False, None, "instant"),
        # per-step override forces the typed/instant decision regardless of config
        (True, False, True, "choreograph"),
        (True, True, False, "instant"),
        (False, False, True, "type"),
    ],
)
def test_navigate_pill_mode_matrix(interact, type_on_navigate, override, expected) -> None:
    chrome = ChromeConfig(
        enabled=True,
        interact_on_navigate=interact,
        type_on_navigate=type_on_navigate,
    )
    assert navigate_pill_mode(chrome, override) == expected


# --- Compile/render geometry (site_viewport) --------------------------------


def _cfg(chrome: ChromeConfig) -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=1280, height=720),
        tts=TtsConfig(provider="fake", voice="v", lang="pl"),
        chrome=chrome,
    )


def test_site_viewport_full_when_chrome_disabled() -> None:
    assert site_viewport(_cfg(ChromeConfig(enabled=False))) == (1280, 720)


def test_site_viewport_reduced_by_bar_height_when_chrome_enabled() -> None:
    assert site_viewport(_cfg(ChromeConfig(enabled=True, height=56))) == (1280, 664)
    assert site_viewport(_cfg(ChromeConfig(enabled=True, height=80))) == (1280, 640)


# --- Shell navigate choreography (frame-sourced pill) -----------------------


@dataclass
class FakeSiteFrame:
    url: str = "about:blank"


@dataclass
class FakePage:
    url: str = SHELL_URL

    async def evaluate(self, script, arg=None):  # pragma: no cover - unused on shell path
        return None


class FakeOverlay:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.pos = (0.0, 0.0)

    async def ensure(self, page) -> None:
        self.events.append(("overlay.ensure", page.url))


class FakeShellChrome:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    async def ensure_shell(self, page) -> None:
        self.events.append(("ensure_shell", page.url))

    async def type_url(self, page, overlay, url, *, seed, choreograph) -> None:
        self.events.append(("type_url", url, seed, choreograph))

    async def set_url_shell(self, page, url) -> None:
        self.events.append(("set_url_shell", url))

    async def set_url(self, page, url, *, animate=True) -> None:  # legacy — must not fire
        self.events.append(("legacy_set_url", url, animate))


@dataclass
class FakeShellRecorder:
    page: FakePage
    frame: FakeSiteFrame
    events: list[tuple]
    final_url: str

    async def navigate(self, url: str) -> None:
        self.events.append(("navigate", url))
        self.frame.url = self.final_url


def _shell_scenario(step: Step, chrome: ChromeConfig) -> Scenario:
    return Scenario(
        config=Config(
            title="t",
            viewport=Viewport(width=1280, height=720),
            tts=TtsConfig(provider="fake", voice="v", lang="pl"),
            baseUrl="https://example.com/base/",
            chrome=chrome,
        ),
        steps=[step],
    )


async def _run_shell(step: Step, chrome: ChromeConfig, final_url: str) -> list[tuple]:
    events: list[tuple] = []
    page = FakePage()
    frame = FakeSiteFrame()
    overlay = FakeOverlay(events)
    chrome_ctl = FakeShellChrome(events)
    recorder = FakeShellRecorder(page, frame, events, final_url)
    scenario = _shell_scenario(step, chrome)

    await _render_step(
        page, recorder, overlay, chrome_ctl, scenario, step, "navigate", 3, None, 0.0, {}
    )
    return events


async def test_shell_navigate_choreographs_then_reflects_redirected_frame_url() -> None:
    events = await _run_shell(
        Step(navigate={"url": "login", "type": True}),
        ChromeConfig(enabled=True),
        final_url="https://redirected.example/final",
    )

    # choreography types the resolved URL before navigation, then the pill is
    # sourced from the SITE FRAME url (the redirected final), never page.url.
    assert events == [
        ("ensure_shell", SHELL_URL),
        ("overlay.ensure", SHELL_URL),
        ("type_url", "https://example.com/base/login", "https://example.com/base/login:3", True),
        ("navigate", "https://example.com/base/login"),
        ("set_url_shell", "https://redirected.example/final"),
        ("ensure_shell", SHELL_URL),
        ("overlay.ensure", SHELL_URL),
    ]
    # never falls back to the legacy in-DOM pill on the shell page
    assert not any(event[0] == "legacy_set_url" for event in events)


async def test_shell_navigate_type_mode_skips_pointer_choreography() -> None:
    events = await _run_shell(
        Step(navigate="login"),
        ChromeConfig(enabled=True, interact_on_navigate=False),
        final_url="https://example.com/base/login",
    )

    type_events = [event for event in events if event[0] == "type_url"]
    assert type_events == [
        ("type_url", "https://example.com/base/login", "https://example.com/base/login:3", False),
    ]


async def test_shell_navigate_instant_mode_only_reflects_final_url() -> None:
    events = await _run_shell(
        Step(navigate={"url": "login", "type": False}),
        ChromeConfig(enabled=True),
        final_url="https://redirected.example/final",
    )

    assert not any(event[0] == "type_url" for event in events)
    assert ("set_url_shell", "https://redirected.example/final") in events


async def test_shell_navigate_hidden_url_skips_all_pill_updates() -> None:
    events = await _run_shell(
        Step(navigate={"url": "login", "type": True}),
        ChromeConfig(enabled=True, show_url=False),
        final_url="https://redirected.example/final",
    )

    assert not any(event[0] in {"type_url", "set_url_shell", "legacy_set_url"} for event in events)
    assert ("navigate", "https://example.com/base/login") in events
