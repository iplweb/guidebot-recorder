"""Unit tests for the live capture pass, driven with fakes (no real browser)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.geometry import ray_exit
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step, WaitUntil, WhenBlock
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.geometry import ellipse_around, fit_to_bounds
from guidebot_recorder.recorder.recorder import (
    OPTION_MISSING,
    UNDRIVABLE,
    PointResult,
    SelectDriveError,
    SelectReveal,
)
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import SelectsNotReadyError

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


#: Geometry a `FakeRecorder.select` reports for the option row it "opened".
FAKE_ROW = {"x": 40.0, "y": 100.0, "width": 200.0, "height": 24.0}
FAKE_ROW_CENTER = (140.0, 112.0)
#: ...and for the control the reader is in, which the same call reports.
FAKE_CONTROL = {"x": 30.0, "y": 60.0, "width": 220.0, "height": 21.0}
FAKE_CONTROL_CENTER = (140.0, 70.5)


class FakeRecorder:
    #: The row a `select` reports to its `on_revealed` hook; `None` stands for
    #: `mode: native`, which unfurls nothing and so has no row to mark.
    row: dict | None = FAKE_ROW

    def __init__(self, events: list[str] | None = None):
        self.frame = object()
        self.events = events if events is not None else []
        self.wait_for_calls: list[tuple] = []
        self.wait_seconds_calls: list[float] = []
        self.navigate_calls: list[str] = []
        self.readiness_calls: list[str] = []
        self.point_calls: list = []
        self.scroll_calls: list = []
        self.select_calls: list[tuple] = []
        self.last_locator: FakeLocator | None = None

    async def select(self, target, option, *, native=False, ripple=True, on_revealed=None):
        """Stand in for the real choreography: open, let the caller look, commit.

        The order is the contract the PDF guide depends on, so the fake keeps it
        rather than just recording the call.
        """

        self.select_calls.append((target, option, native, ripple))
        self.events.append("open")
        if on_revealed is not None:
            await on_revealed(
                SelectReveal(
                    FAKE_CONTROL,
                    FAKE_CONTROL_CENTER,
                    self.row,
                    None if self.row is None else FAKE_ROW_CENTER,
                )
            )
        self.events.append(f"select:{option}")

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


def _select_scenario_and_action(mode: str | None = None):
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(select=Select(from_="zakres", option="Zakres lat", mode=mode))],
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    return scenario, action


async def test_select_step_is_photographed_while_its_list_is_open(tmp_path, monkeypatch):
    """The frame lands *between* opening the list and choosing from it.

    This is the whole feature. A frame taken after the choice — which is what
    the guide used to take — shows a collapsed control that has silently changed
    value: no list, no click target, nothing that reads as a dropdown.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action()
    events: list[str] = []
    recorder = FakeRecorder(events)
    page = FakePage(events)
    pages = await capture_pages(
        scenario, _compiled([action]), page, recorder, tmp_path / "shots", timeout=15.0
    )
    assert events == ["open", "screenshot", "select:Zakres lat"]
    assert len(pages) == 1
    assert pages[0].screenshot is not None


async def test_select_marks_the_option_row_and_frames_the_control(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action()
    pages = await capture_pages(
        scenario,
        _compiled([action]),
        FakePage(),
        FakeRecorder(),
        tmp_path / "shots",
        timeout=15.0,
    )
    annotations = {a.kind: a for a in pages[0].annotations}
    assert set(annotations) == {"frame", "click"}
    # The frame is the control the reader is in — NOT the row, and not the box
    # the cursor approach measured, which by frame time is stale.
    rect = annotations["frame"]
    assert (rect.x, rect.y, rect.w, rect.h) == (
        FAKE_CONTROL["x"],
        FAKE_CONTROL["y"],
        FAKE_CONTROL["width"],
        FAKE_CONTROL["height"],
    )
    assert (annotations["click"].cx, annotations["click"].cy) == FAKE_ROW_CENTER


async def test_the_next_steps_arrow_starts_from_the_option_row(tmp_path, monkeypatch):
    """The reader's eye was left on the row, so that is where the next arrow begins."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(select=Select(from_="zakres", option="Zakres lat")),
            Step(click="przycisk zapisu"),
        ],
    )
    select_action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    click_action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    pages = await capture_pages(
        scenario,
        _compiled([select_action, click_action]),
        FakePage(),
        FakeRecorder(),
        tmp_path / "shots",
        timeout=15.0,
    )
    arrow = next(a for a in pages[1].annotations if a.kind == "arrow")
    # Arrows are clipped to the rim of the shape each end sits in, so the start
    # is on the *row's* boundary — leaving it through the top edge, since the
    # next target is above and to the left. The control's own box (`FAKE_CONTROL`,
    # y=60) shares no edge with it, so this cannot pass for the wrong shape.
    assert arrow.y1 == pytest.approx(FAKE_ROW["y"])
    assert FAKE_ROW["x"] <= arrow.x1 <= FAKE_ROW["x"] + FAKE_ROW["width"]


async def test_native_mode_keeps_the_collapsed_frame_and_its_single_mark(tmp_path, monkeypatch):
    """A `select` with nothing to unfurl must not become an error or grow a star."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action(mode="native")
    recorder = FakeRecorder()
    recorder.row = None
    pages = await capture_pages(
        scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert [call[2] for call in recorder.select_calls] == [True]  # `native=True`
    assert {a.kind for a in pages[0].annotations} == {"frame"}


async def test_the_still_capture_asks_for_no_click_ring(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action()
    recorder = FakeRecorder()
    await capture_pages(
        scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert [call[3] for call in recorder.select_calls] == [False]  # `ripple=False`


async def test_an_undriveable_select_fails_with_its_own_banner(tmp_path, monkeypatch):
    """`SelectDriveError` is a step failure, so it arrives with `plik:linia`."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action()

    class RefusingRecorder(FakeRecorder):
        async def select(self, *_args, **_kwargs):
            raise SelectDriveError("strona ukryła select#z i nie znaleziono kontrolki")

    with pytest.raises(GuideError) as excinfo:
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            RefusingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )
    assert "nie znaleziono kontrolki" in str(excinfo.value)
    assert "krok 1/1" in str(excinfo.value)


async def test_not_visible_on_a_select_is_answered_by_the_recorders_diagnosis(
    tmp_path, monkeypatch
):
    """`cel jest niewidoczny` is shared with click/hover/type and says nothing
    about *why* a dropdown cannot be shown. For a `select` the verdict has one
    cause, and the recorder already words it for the render."""

    monkeypatch.setattr(capture, "reuse_failure", _async_reason("not_visible"))
    scenario, action = _select_scenario_and_action()

    class DiagnosingRecorder(FakeRecorder):
        async def diagnose_select(self, target, option):
            return SelectDriveError(
                f'strona ukryła select#z i nie znaleziono widocznej kontrolki dla opcji „{option}"'
            )

    with pytest.raises(GuideError) as excinfo:
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            DiagnosingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )
    message = str(excinfo.value)
    assert "nie znaleziono widocznej kontrolki" in message
    assert "Zakres lat" in message
    assert "cel jest niewidoczny" not in message


async def test_a_non_select_reuse_failure_keeps_the_shared_wording(tmp_path, monkeypatch):
    """The diagnosis is scoped to `select`; a hidden button still reads as before."""

    monkeypatch.setattr(capture, "reuse_failure", _async_reason("not_visible"))
    scenario, action = _click_scenario_and_action()
    with pytest.raises(GuideError, match="cel jest niewidoczny"):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            FakeRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )


class FakeSelects:
    """Stand-in for the shim controller: records the frames it was asked about."""

    def __init__(self, error: Exception | None = None):
        self.waited: list = []
        self._error = error

    async def wait_ready(self, frame):
        self.waited.append(frame)
        if self._error is not None:
            raise self._error


async def test_the_readiness_barrier_is_taken_after_navigation(tmp_path):
    """Everything downstream either photographs the document or drives a select
    in it, and both must see the shimmed DOM compile resolved against."""

    scenario = Scenario(config=_cfg(), steps=[Step(navigate="/panel")])
    recorder = FakeRecorder()
    selects = FakeSelects()
    await capture_pages(
        scenario,
        _compiled([None]),
        FakePage(),
        recorder,
        tmp_path / "shots",
        timeout=15.0,
        selects=selects,
    )
    assert selects.waited == [recorder.frame]


async def test_a_wedged_widget_stops_the_run_with_a_located_banner(tmp_path):
    scenario = Scenario(config=_cfg(), steps=[Step(navigate="/panel")])
    selects = FakeSelects(SelectsNotReadyError("widget select nie zgłosił gotowości"))
    with pytest.raises(GuideError) as excinfo:
        await capture_pages(
            scenario,
            _compiled([None]),
            FakePage(),
            FakeRecorder(),
            tmp_path / "shots",
            timeout=15.0,
            selects=selects,
        )
    assert "nie zgłosił gotowości" in str(excinfo.value)
    assert "krok 1/1" in str(excinfo.value)


async def test_native_mode_installs_no_controller_and_waits_for_nothing(tmp_path, monkeypatch):
    """`selects=None` is `config.selects.mode: native`: there is no widget to wait on."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(config=_cfg(), steps=[Step(navigate="/panel")])
    await capture_pages(
        scenario,
        _compiled([None]),
        FakePage(),
        FakeRecorder(),
        tmp_path / "shots",
        timeout=15.0,
    )  # no `selects=`: must not raise


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
    """Points at the control fine, then refuses to choose the option.

    The DOM shape behind a vanished option: the `<select>` is there and `point`
    succeeds, so the `try/except` around `point` never sees the failure that
    actually happens — it comes out of the choreography, one call later.

    `reason` is what the recorder tells the caller the refusal *means*, and the
    default here is the only one an optional step may act on.
    """

    reason = OPTION_MISSING

    async def select(self, target, option, *, native=False, ripple=True, on_revealed=None):
        self.select_calls.append((target, option, native, ripple))
        raise SelectDriveError(
            f'lista select#zakres nie zawiera opcji „{option}"', reason=self.reason
        )


class UndrivableSelectRecorder(SelectFailingRecorder):
    """The same shape of failure for a reason that is not the option's absence."""

    reason = UNDRIVABLE


async def test_optional_select_with_a_missing_option_skips_the_step(tmp_path, monkeypatch):
    """`optional: true` has to cover the select, not just the pointing.

    Optional steps skip reuse validation entirely, so a vanished option surfaces
    from the drive itself. Guarding only `recorder.point` meant an optional
    select could still take the whole guide down.
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
    with pytest.raises(GuideError, match="nie zawiera opcji"):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            SelectFailingRecorder(),
            tmp_path / "shots",
            timeout=15.0,
        )


async def test_optional_select_still_fails_when_the_control_cannot_be_driven(tmp_path, monkeypatch):
    """`optional` means "if the option is on offer", not "if the step works".

    The trap this guards: the skip and the loud failures arrive through the same
    exception type, so a resolution that catches `SelectDriveError` wholesale
    reads *every* broken dropdown — a click that did not take, a widget with
    nothing to unfurl, a shim removed mid-step — as "the option was not there"
    and drops the step from the PDF without a word. That is precisely the silent
    unwatchable output the choreography was written to make impossible, and it
    would be invisible: the guide would succeed.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(select=Select(from_="zakres", option="Zakres lat"), optional=True)],
    )
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    with pytest.raises(GuideError, match="nie zawiera opcji"):
        await capture_pages(
            scenario,
            _compiled([action]),
            FakePage(),
            UndrivableSelectRecorder(),
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


class SequenceRecorder(FakeRecorder):
    """`point` walks a list of boxes, so consecutive targets sit apart on the page.

    `FakeRecorder` puts every target in the same 10x10 box, which is enough for
    "is there an arrow?" but not for "where does it start?" — two targets in the
    same spot overlap, and an arrow between them is dropped as degenerate.
    """

    def __init__(self, boxes: list[dict], events: list[str] | None = None):
        super().__init__(events)
        self._boxes = list(boxes)

    async def point(self, target, ripple=False):
        box = self._boxes[len(self.point_calls)]
        self.point_calls.append(target)
        locator = FakeLocator(self.events)
        self.last_locator = locator
        return PointResult(
            locator=locator,
            box=box,
            center=(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2),
        )


#: Two targets far enough apart that the clipped arrow between them survives
#: `MIN_ARROW`; the gap runs along x, so the clipped ends are the vertical edges.
_TWO_BOXES = [
    {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
    {"x": 400.0, "y": 0.0, "width": 100.0, "height": 100.0},
]


async def test_arrow_starts_at_the_edge_of_the_previous_target(tmp_path, monkeypatch):
    """The next step's arrow needs the *shape* of the previous target, not just its centre.

    Started in the middle of the previous target the arrow crosses all of it and
    reads as a strikethrough, so capture remembers `prev_shape` alongside
    `prev_cursor` and hands it to `annotations_for`, which clips the start
    against it.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(), steps=[Step(click="pierwszy przycisk"), Step(click="drugi przycisk")]
    )
    actions = [
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        )
        for _ in range(2)
    ]
    recorder = SequenceRecorder(_TWO_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert len(pages) == 2
    arrow = next(a for a in pages[1].annotations if a.kind == "arrow")
    # 50.0 would be the centre of the first target — the pre-clipping behaviour.
    assert arrow.x1 == pytest.approx(100.0)  # right edge of the first box
    assert arrow.x2 == pytest.approx(400.0)  # left edge of the second box


#: Highlight target flush against the right edge, then a click far to the left.
#: `fit_to_bounds` pushes the highlight ellipse inward off the edge, so its fitted
#: rim sits at a different x than the raw one — enough to tell the two apart.
_HIGHLIGHT_EDGE_BOXES = [
    {"x": 1200.0, "y": 340.0, "width": 100.0, "height": 40.0},  # highlight, przy prawej krawędzi
    {"x": 100.0, "y": 340.0, "width": 100.0, "height": 40.0},  # klik, po lewej
]


async def test_arrow_after_a_highlight_starts_on_the_fitted_ellipse(tmp_path, monkeypatch):
    """`prev_shape` po kroku `highlight` musi nieść elipsę DOPASOWANĄ do kadru.

    `capture` zapamiętuje kształt przez `target_shape(..., bounds=size)`. Bez
    `bounds` zapamiętałby elipsę niedopasowaną — a przy celu tuż przy krawędzi
    kadru `fit_to_bounds` przesuwa ją na tyle, że grot następnej strzałki
    startowałby w innym miejscu. Ten test przypina, że start leży na elipsie
    dopasowanej, nie surowej.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(highlight="tabela"), Step(click="drugi przycisk")],
    )
    actions = [
        CachedAction(
            action="highlight",
            target=_target(),
            expect="none",
            fingerprint=_fp(command_kind="highlight"),
        ),
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
    ]
    recorder = SequenceRecorder(_HIGHLIGHT_EDGE_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )

    assert len(pages) == 2
    arrow = next(a for a in pages[1].annotations if a.kind == "arrow")

    padding = _cfg().highlight.padding
    hl_box, click_box = _HIGHLIGHT_EDGE_BOXES
    hl_center = (hl_box["x"] + hl_box["width"] / 2, hl_box["y"] + hl_box["height"] / 2)
    click_center = (
        click_box["x"] + click_box["width"] / 2,
        click_box["y"] + click_box["height"] / 2,
    )

    fitted = fit_to_bounds(ellipse_around(hl_box, padding), width=1280.0, height=720.0)
    unfitted = ellipse_around(hl_box, padding)
    fitted_start = ray_exit(hl_center, click_center, fitted)
    unfitted_start = ray_exit(hl_center, click_center, unfitted)

    # dobór pudełek jest sensowny tylko, jeśli dopasowanie realnie przesuwa start —
    # inaczej test przeszedłby też na elipsie niedopasowanej i niczego by nie chronił
    assert fitted_start[0] != pytest.approx(unfitted_start[0])
    assert (arrow.x1, arrow.y1) == pytest.approx(fitted_start)


def _recording_annotations(calls):
    """Zamiast budować adnotacje, zapisuje `prev_shape` przekazany do każdego kroku akcji."""

    def _f(
        action,
        *,
        prev_cursor,
        prev_shape=None,
        center,
        box,
        row_box=None,
        row_center=None,
        mark=None,
        bounds=None,
    ):
        calls.append(prev_shape)
        return []

    return _f


async def test_navigate_hands_the_next_action_a_cleared_prev_shape(tmp_path, monkeypatch):
    """Zerowanie `prev_shape` po `navigate` musi być OBSERWOWALNE.

    `annotations_for` nie zagląda do `prev_shape`, gdy `prev_cursor is None`, więc
    test na samym braku strzałki przechodzi także po usunięciu `prev_shape = None`.
    Tu podglądamy wprost kwarg dany `annotations_for`: krok po `navigate` musi
    dostać `prev_shape=None`, a nie kształt sprzed przeładowania strony.
    """

    calls: list = []
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    monkeypatch.setattr(capture, "annotations_for", _recording_annotations(calls))
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(navigate="https://example.com/inna"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
        None,
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
    ]
    await capture_pages(
        scenario, _compiled(actions), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )

    assert len(calls) == 2  # dwa kroki akcji; `navigate` nie woła `annotations_for`
    assert calls[0] is None  # pierwszy krok — poprzedniego kształtu jeszcze nie ma
    assert calls[1] is None  # po `navigate` kształt sprzed przeładowania wyzerowany


async def test_scroll_hands_the_next_action_a_cleared_prev_shape(tmp_path, monkeypatch):
    """To samo dla gałęzi `scroll` — dziś nie ma żadnego testu na to zerowanie."""

    calls: list = []
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    monkeypatch.setattr(capture, "annotations_for", _recording_annotations(calls))
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(scroll="down"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
        None,
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
    ]
    await capture_pages(
        scenario, _compiled(actions), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )

    assert len(calls) == 2
    assert calls[1] is None  # po `scroll` kształt sprzed przewinięcia wyzerowany


async def test_shape_memory_resets_after_navigate(tmp_path, monkeypatch):
    """A navigate clears the remembered shape together with the cursor.

    Kept across a page load, `prev_shape` would clip the next arrow against a
    box that belongs to a screenshot the reader never sees. Both are dropped, so
    the step after the navigate opens a fresh arrow-less page.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(navigate="https://example.com/inna"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
        None,
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
    ]
    recorder = SequenceRecorder(_TWO_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert [p.kind for p in pages] == ["step", "navigate", "step"]
    assert all(a.kind != "arrow" for a in pages[2].annotations)


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
