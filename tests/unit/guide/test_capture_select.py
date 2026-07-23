"""Select-step handling: the open-look-commit choreography, the readiness
barrier, reuse validation of the wanted option, and the drive failures a
select can raise. Driven with fakes (no real browser).

Per-step-kind replay lives in ``test_capture_replay.py``; error and pause
banners in ``test_capture_errors.py``; cursor-trail annotations in
``test_capture_trail.py``. Shared fakes come from ``_capture_helpers.py``.
"""

from __future__ import annotations

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Select, Step
from guidebot_recorder.recorder.recorder import UNDRIVABLE, SelectDriveError
from guidebot_recorder.selects import SelectsNotReadyError

from ._capture_helpers import (
    FAKE_CONTROL,
    FAKE_ROW,
    FAKE_ROW_CENTER,
    FakePage,
    FakeRecorder,
    SelectFailingRecorder,
    _async_none,
    _async_reason,
    _cfg,
    _click_scenario_and_action,
    _compiled,
    _fp,
    _target,
)


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
