"""Per-step-kind replay: gate/wait/scroll/text, driven with fakes (no browser).

Select-step handling lives in ``test_capture_select.py``; error and pause
banners in ``test_capture_errors.py``; cursor-trail and arrow annotations in
``test_capture_trail.py``. Shared fakes come from ``_capture_helpers.py``.
"""

from __future__ import annotations

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil, WhenBlock

from ._capture_helpers import (
    FakePage,
    FakeRecorder,
    _async_none,
    _cfg,
    _compiled,
    _fp,
    _target,
)


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


async def test_verbose_reports_progress_for_every_step(tmp_path, monkeypatch, capsys):
    """`-v` ma pokazywać, że coś się dzieje — pasek plus rodzaj każdego kroku.

    Wcześniej `verbose` sterowało wyłącznie komunikatami o pomijaniu, więc
    zdrowy scenariusz nie wypisywał ani linijki aż do końca budowania PDF-a.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(say="Zaczynamy."), Step(click="przycisk zapisu"), Step(wait=0.0)],
    )
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    await capture_pages(
        scenario,
        _compiled([None, action, None]),
        FakePage(),
        FakeRecorder(),
        tmp_path / "shots",
        timeout=15.0,
        verbose=True,
    )
    out = capsys.readouterr()
    assert "[1/3] text" in out.out
    assert "[2/3] action" in out.out
    assert "[3/3] wait" in out.out
    assert "guide" in out.err  # pasek tqdm idzie na stderr


async def test_quiet_run_prints_nothing(tmp_path, monkeypatch, capsys):
    """Bez `-v` ani pasek, ani opis kroku nie mogą zaśmiecić wyjścia."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    await capture_pages(
        scenario,
        _compiled([action]),
        FakePage(),
        FakeRecorder(),
        tmp_path / "shots",
        timeout=15.0,
    )
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


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
    events: list[str] = []
    recorder = FakeRecorder(events)
    page = FakePage(events)
    pages = await capture_pages(
        scenario, _compiled([None]), page, recorder, tmp_path / "shots", timeout=15.0
    )
    assert len(recorder.scroll_calls) == 1
    assert len(pages) == 1
    assert pages[0].text == "Przewijamy w dół"
    assert pages[0].screenshot is not None
    # the scroll must land BEFORE the screenshot: otherwise the PDF page would
    # show the frame from before the page moved, not the state being described.
    assert events == ["scroll:down", "screenshot"]
