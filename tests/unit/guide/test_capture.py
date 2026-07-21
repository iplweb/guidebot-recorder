"""Unit tests for the live capture pass, driven with fakes (no real browser)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step, WaitUntil, WhenBlock
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.recorder import PointResult, SelectDriveError, SelectReveal
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
    assert set(annotations) == {"selected", "click"}
    # The rectangle is the control the reader is in — NOT the row, and not the
    # box the cursor approach measured, which by frame time is stale.
    rect = annotations["selected"]
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
    assert (arrow.x1, arrow.y1) == FAKE_ROW_CENTER


async def test_native_mode_keeps_the_collapsed_frame_and_its_single_mark(tmp_path, monkeypatch):
    """A `select` with nothing to unfurl must not become an error or grow a circle."""

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario, action = _select_scenario_and_action(mode="native")
    recorder = FakeRecorder()
    recorder.row = None
    pages = await capture_pages(
        scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert [call[2] for call in recorder.select_calls] == [True]  # `native=True`
    assert {a.kind for a in pages[0].annotations} == {"selected"}


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
    This must raise a GuideError telling the user to re-freeze, not silently
    call select_option with a nonexistent option.
    """
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(config=_cfg(), steps=[Step(click="jakiś przycisk")])
    action = CachedAction(
        action="select", target=_target(), expect="none", fingerprint=_fp(command_kind="select")
    )
    recorder = FakeRecorder()
    with pytest.raises(GuideError, match="compile --force"):
        await capture_pages(
            scenario, _compiled([action]), FakePage(), recorder, tmp_path / "shots", timeout=15.0
        )


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
