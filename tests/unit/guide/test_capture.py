"""Unit tests for the live capture pass, driven with fakes (no real browser)."""

from __future__ import annotations

from pathlib import Path

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step, WaitUntil, WhenBlock
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.recorder import PointResult


def _cfg():
    return Config(
        title="t",
        viewport=Viewport(width=1280, height=720),
        tts=TtsConfig(provider="p", voice="v", lang="eng"),
        base_url="https://example.com",
    )


def _fp(command_kind="click"):
    return Fingerprint(command_kind=command_kind, compiled_from="x", expect="none", config_hash="c")


def _target():
    return RoleTarget(role="button", name="x")


class FakeLocator:
    def __init__(self, events: list[str] | None = None):
        self.events = events if events is not None else []
        self.fill_calls: list[str] = []
        self.click_calls = 0
        self.hover_calls = 0
        self.select_calls: list[str] = []

    async def fill(self, text):
        self.fill_calls.append(text)
        self.events.append(f"fill:{text}")

    async def click(self):
        self.click_calls += 1
        self.events.append("click")

    async def hover(self):
        self.hover_calls += 1
        self.events.append("hover")

    async def select_option(self, label):
        self.select_calls.append(label)
        self.events.append(f"select:{label}")


class FakeRecorder:
    def __init__(self, events: list[str] | None = None):
        self.frame = object()
        self.events = events if events is not None else []
        self.wait_for_calls: list[tuple] = []
        self.wait_seconds_calls: list[float] = []
        self.navigate_calls: list[str] = []
        self.readiness_calls: list[str] = []
        self.point_calls: list = []
        self.scroll_calls: list = []
        self.last_locator: FakeLocator | None = None

    async def wait_for(self, target, state, timeout):
        self.wait_for_calls.append((target, state, timeout))

    async def wait_seconds(self, seconds):
        self.wait_seconds_calls.append(seconds)

    async def navigate(self, url):
        self.navigate_calls.append(url)

    async def apply_readiness(self, expect):
        self.readiness_calls.append(expect)

    async def point(self, target, ripple=False):
        self.point_calls.append(target)
        locator = FakeLocator(self.events)
        self.last_locator = locator
        return PointResult(
            locator=locator, box={"x": 0, "y": 0, "width": 10, "height": 10}, center=(5.0, 5.0)
        )

    async def scroll(self, spec):
        self.scroll_calls.append(spec)
        self.events.append(f"scroll:{spec.to}")


class FakePage:
    def __init__(self, events: list[str] | None = None):
        self.viewport_size = {"width": 1280, "height": 720}
        self.events = events if events is not None else []

    async def screenshot(self, path):
        Path(path).write_bytes(b"fake")
        self.events.append("screenshot")


async def test_wait_until_step_awaits_the_frozen_waitfor(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", None)  # unused for this path; must not be called
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(wait=WaitUntil(until="spinnerowi zniknąć", state="hidden", timeout=7.0))],
    )
    target = _target()
    action = CachedAction(
        action="waitFor",
        target=target,
        state="hidden",
        expect="none",
        fingerprint=_fp(command_kind="wait"),
    )
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert recorder.wait_for_calls == [(target, "hidden", 7.0)]
    assert pages == []


async def test_numeric_wait_still_calls_wait_seconds(tmp_path):
    scenario = Scenario(config=_cfg(), steps=[Step(wait=2.5)])
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario, _compiled([None]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert recorder.wait_seconds_calls == [2.5]
    assert pages == []


async def test_mandatory_click_with_stale_identity_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_reason("identity_mismatch"))
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError, match="compile --force"):
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )


async def test_reuse_failure_not_found_has_no_force_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_reason("not_found"))
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError) as exc_info:
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )
    assert "celu nie ma na stronie" in str(exc_info.value)
    assert "compile --force" not in str(exc_info.value)


async def test_type_with_no_frozen_text_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(config=_cfg(), steps=[Step(teach="wpisz coś w pole")])
    action = CachedAction(
        action="type",
        target=_target(),
        expect="none",
        input_text=None,
        fingerprint=_fp(command_kind="enterText"),
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError, match="compile"):
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )


async def test_gate_honors_hidden_state_and_the_gates_own_timeout(tmp_path, monkeypatch):
    """A `when: {state: hidden}` gate must wait for hidden, not the hardcoded

    "visible", and must use the gate's own WaitUntil timeout — not the guide's
    overall step timeout. Regression for a branch that inverted `hidden` gates.
    """
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[WhenBlock(when="a spinner", state="hidden", timeout=3.0, steps=[Step(click="ok")])],
    )
    gate_target = _target()
    gate_action = CachedAction(
        action="waitFor",
        target=gate_target,
        state="hidden",
        expect="none",
        fingerprint=_fp(command_kind="wait"),
    )
    child_action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    recorder = FakeRecorder()
    await capture_pages(
        scenario,
        _compiled([gate_action, child_action]),
        FakePage(),
        recorder,
        tmp_path / "shots",
        timeout=15.0,  # deliberately different from the gate's own 3.0s timeout
    )
    assert recorder.wait_for_calls == [(gate_target, "hidden", 3.0)]


async def test_select_step_picks_option_then_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(), steps=[Step(select=Select(from_="zakres", option="Zakres lat"))]
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    events: list[str] = []
    recorder = FakeRecorder(events)
    page = FakePage(events)
    pages = await capture_pages(
        scenario, _compiled([action]), page, recorder, tmp_path / "shots", timeout=15.0
    )
    assert recorder.last_locator.select_calls == ["Zakres lat"]
    # select_option happens BEFORE the screenshot: the native option list closes
    # only after the value is chosen, so the frame must be taken afterwards.
    assert events == ["select:Zakres lat", "screenshot"]
    assert len(pages) == 1
    assert any(a.kind == "selected" for a in pages[0].annotations)


async def test_scroll_without_say_calls_recorder_but_makes_no_page(tmp_path):
    scenario = Scenario(config=_cfg(), steps=[Step(scroll="down")])
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario, _compiled([None]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert len(recorder.scroll_calls) == 1
    assert recorder.scroll_calls[0].to == "down"
    assert pages == []


async def test_scroll_with_say_produces_one_page_with_text_and_screenshot(tmp_path):
    scenario = Scenario(config=_cfg(), steps=[Step(scroll="down", say="Przewijamy w dół")])
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario, _compiled([None]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert len(recorder.scroll_calls) == 1
    assert len(pages) == 1
    assert pages[0].text == "Przewijamy w dół"
    assert pages[0].screenshot is not None


async def test_cursor_resets_after_scroll(tmp_path, monkeypatch):
    """A prior action leaves a cursor position; a scroll must clear it.

    Without the reset, the action AFTER the scroll would draw an arrow from
    the stale pre-scroll coordinates to its own (identical, per FakeRecorder)
    center — a sequence that needs an action BEFORE the scroll to be
    observable at all.
    """
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(scroll="down"),
            Step(click="drugi przycisk"),
        ],
    )
    action1 = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    action2 = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario,
        _compiled([action1, None, action2]),
        FakePage(),
        recorder,
        tmp_path / "shots",
        timeout=15.0,
    )
    assert len(pages) == 2
    assert all(a.kind != "arrow" for a in pages[1].annotations)


async def _async_none(*_args, **_kwargs):
    return None


def _async_reason(reason):
    async def _f(*_args, **_kwargs):
        return reason

    return _f


class _Compiled:
    def __init__(self, actions):
        self.actions = actions


def _compiled(actions):
    return _Compiled(actions)
