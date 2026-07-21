"""Unit tests for the live capture pass, driven with fakes (no real browser)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step, WaitUntil, WhenBlock
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.recorder import PointResult
from guidebot_recorder.scenario.loader import load_scenario

#: Scenariusz z pliku — jedyna droga do mapy źródła (`Scenario.source`), więc
#: jedyna, w której bannery `guide` mogą nieść `plik:linia`.
SCENARIO_YAML = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 1280, height: 720}
      tts: {provider: p, voice: v, lang: eng}
      baseUrl: "https://example.com"
    steps:
      - say: "Zaczynamy."
      - click: "przycisk zapisu"
    """
)
#: linia `- click: "przycisk zapisu"` w :data:`SCENARIO_YAML`
CLICK_LINE = 8


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


def _recording_reuse_failure(calls, reason=None):
    """Stand-in for `reuse_failure` that records the `option=` it was handed."""

    async def _f(_frame, _cached, option=None):
        calls.append(option)
        return reason

    return _f


async def test_select_step_hands_the_wanted_option_to_reuse_validation(tmp_path, monkeypatch):
    """`guide` is the one caller that knows the label — it has to pass it on.

    Without it `validate_compile_time` only checks the element, so an option
    that vanished from the DOM between `compile` and `guide` is not caught here
    and instead hangs `select_option` until Playwright's timeout.
    """

    calls: list[str | None] = []
    monkeypatch.setattr(capture, "reuse_failure", _recording_reuse_failure(calls))
    scenario = Scenario(
        config=_cfg(), steps=[Step(select=Select(from_="zakres", option="Zakres lat"))]
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    await capture_pages(
        scenario, _compiled([action]), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )
    assert calls == ["Zakres lat"]


async def test_non_select_step_passes_no_option_to_reuse_validation(tmp_path, monkeypatch):
    """Only a `select` step has a label; everything else must validate as before."""

    calls: list[str | None] = []
    monkeypatch.setattr(capture, "reuse_failure", _recording_reuse_failure(calls))
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    await capture_pages(
        scenario, _compiled([action]), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )
    assert calls == [None]


async def test_select_with_a_vanished_option_fails_with_a_polish_sentence(tmp_path, monkeypatch):
    """The payoff: a sentence before the action runs, not a Playwright traceback.

    `cli.py` only catches `GuideError`, so the alternative the user saw was an
    English stack trace after a 10–15s wait.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_reason("option_missing"))
    scenario = Scenario(
        config=_cfg(), steps=[Step(select=Select(from_="zakres", option="Zakres lat"))]
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError) as exc_info:
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )
    assert "nie ma żądanej opcji" in str(exc_info.value)
    # it must fail *before* the control is driven — that is the whole point
    assert recorder.point_calls == []


class SelectFailingRecorder(FakeRecorder):
    """Points at the control fine, then fails to choose the option.

    The DOM shape behind `select_option` timing out: the `<select>` is there and
    `point` succeeds, so the existing `try/except` around `point` never sees the
    failure that actually happens.
    """

    async def point(self, target, ripple=False):
        result = await super().point(target, ripple=ripple)

        async def _boom(label):
            raise PlaywrightError(f"Timeout 15000ms exceeded waiting for option {label!r}")

        result.locator.select_option = _boom
        return result


async def test_optional_select_with_a_missing_option_skips_the_step(tmp_path, monkeypatch):
    """`optional: true` has to cover the select, not just the pointing.

    Optional steps skip reuse validation entirely, so a missing option surfaces
    as a `select_option` failure. Guarding only `recorder.point` meant an
    optional select could still take the whole guide down.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(select=Select(from_="zakres", option="Zakres lat"), optional=True)],
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    pages = await capture_pages(
        scenario,
        _compiled([action]),
        FakePage(),
        SelectFailingRecorder(),
        tmp_path / "shots",
        timeout=15.0,
    )
    assert pages == []


async def test_mandatory_select_with_a_missing_option_still_raises(tmp_path, monkeypatch):
    """Skipping is what `optional` buys; a mandatory step must still fail loud."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(), steps=[Step(select=Select(from_="zakres", option="Zakres lat"))]
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    with pytest.raises(PlaywrightError):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            SelectFailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )


class _Boom(RuntimeError):
    """A step failure that is neither PlaywrightError nor GuideError."""


class FailingRecorder(FakeRecorder):
    async def point(self, target, ripple=False):
        raise _Boom("krok padł na sekrecie hunter2")


class _RecordingPause:
    """Stand-in for `pause_for_inspection` that records its call arguments."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, page, phase, index, kind, exc, sensitive_values=(), **location):
        self.calls.append((page, phase, index, kind, exc, sensitive_values))
        self.location = location


def _click_scenario_and_action():
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    return scenario, action


def _loaded_scenario(tmp_path):
    """Scenariusz wczytany z pliku — dokładnie jak w `guidebot guide`."""

    path = tmp_path / "flow.scenario.yaml"
    path.write_text(SCENARIO_YAML, encoding="utf-8")
    scenario = load_scenario(path, env={})
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    # krok 0 to `say:` (bez akcji), krok 1 to klikanie
    return scenario, path, _compiled([None, action])


async def test_pause_on_error_pauses_and_reraises_untouched(tmp_path, monkeypatch):
    """A failing step pauses for inspection, then the original exception propagates."""
    pause = _RecordingPause()
    monkeypatch.setattr(capture, "pause_for_inspection", pause)
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _click_scenario_and_action()
    page = FakePage()
    with pytest.raises(_Boom):  # NOT wrapped in GuideError
        await capture_pages(
            scenario,
            _compiled([action]),
            page,
            FailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
            pause_on_error=True,
        )
    assert len(pause.calls) == 1
    called_page, phase, index, kind, exc, _sensitive = pause.calls[0]
    assert called_page is page
    assert phase == "guide"
    assert index == 0
    assert kind == "action"
    assert isinstance(exc, _Boom)
    # scenariusz zbudowany w kodzie nie ma mapy źródła — diagnostyka degraduje
    # się do samego `krok 1/1`, ale kwargi i tak muszą dojść komplet
    assert pause.location == {"total": 1, "location": None, "source": None}


async def test_pause_receives_the_step_location_of_a_loaded_scenario(tmp_path, monkeypatch):
    """Sześć kwargów zmigrowanych w `capture.py` niesie realny span kroku."""

    pause = _RecordingPause()
    monkeypatch.setattr(capture, "pause_for_inspection", pause)
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, _path, compiled = _loaded_scenario(tmp_path)

    with pytest.raises(_Boom):
        await capture_pages(
            scenario,
            compiled,
            FakePage(),
            FailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
            pause_on_error=True,
        )

    assert pause.calls[0][2] == 1  # płaski indeks kroku
    assert pause.location["total"] == 2
    assert pause.location["source"] is scenario.source
    assert pause.location["location"].line == CLICK_LINE


async def test_pause_banner_of_a_loaded_scenario_shows_file_and_line(tmp_path, monkeypatch, capsys):
    """Cała droga: `capture` → `pause_for_inspection` → `step_banner`.

    Bez tego testu `total`/`location`/`source` mogłyby dojechać do
    `pause_for_inspection` i nie zamienić się w nic widocznego.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, path, compiled = _loaded_scenario(tmp_path)

    with pytest.raises(_Boom):
        await capture_pages(
            scenario,
            compiled,
            FakePage(),
            FailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
            pause_on_error=True,
            sensitive_values=("hunter2",),
        )

    printed = capsys.readouterr().out
    assert f"krok 2/2 — {path}:{CLICK_LINE}" in printed
    assert '- click: "przycisk zapisu"' in printed
    assert "hunter2" not in printed  # sekret z treści wyjątku zredagowany


async def test_guide_error_banner_shows_file_and_line(tmp_path, monkeypatch):
    """Komunikat błędu `guide` (nie tylko pauzy) też niesie `plik:linia`."""

    monkeypatch.setattr(capture, "reuse_failure", _async_reason("identity_mismatch"))
    scenario, path, compiled = _loaded_scenario(tmp_path)

    with pytest.raises(GuideError) as excinfo:
        await capture_pages(
            scenario, compiled, FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
        )

    message = str(excinfo.value)
    assert f"krok 2/2 — {path}:{CLICK_LINE}" in message
    assert f'{CLICK_LINE} |   - click: "przycisk zapisu"' in message
    assert "uruchom `compile --force`" in message


async def test_without_pause_on_error_the_helper_is_not_called(tmp_path, monkeypatch):
    pause = _RecordingPause()
    monkeypatch.setattr(capture, "pause_for_inspection", pause)
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _click_scenario_and_action()
    with pytest.raises(_Boom):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            FailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )
    assert pause.calls == []


async def test_pause_receives_the_sensitive_values(tmp_path, monkeypatch):
    pause = _RecordingPause()
    monkeypatch.setattr(capture, "pause_for_inspection", pause)
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _click_scenario_and_action()
    with pytest.raises(_Boom):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            FailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
            pause_on_error=True,
            sensitive_values=("hunter2",),
        )
    assert [call[5] for call in pause.calls] == [("hunter2",)]


async def test_select_action_without_select_step_raises(tmp_path, monkeypatch):
    """The sidecar recorded a `select` action, but the scenario step it maps to
    isn't a select step (e.g. the scenario was edited by hand after freezing).
    This must raise a GuideError telling the user to re-compile, not silently
    call select_option with a nonexistent option.

    A plain `compile` is the right advice: editing the step changed its
    `command_kind`, so the fingerprint no longer matches and the entry is
    re-resolved without `--force`. Recommending `--force` would needlessly
    re-freeze every other step in the scenario.
    """
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(config=_cfg(), steps=[Step(click="jakiś przycisk")])
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError) as exc_info:
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )
    assert "uruchom `compile`" in str(exc_info.value)
    assert "--force" not in str(exc_info.value)


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
