"""Step-failure handling: reuse-failure banners, `pause_on_error`, and the
`plik:linia` step location a loaded scenario carries into the banner. Driven
with fakes (no real browser).

Per-step-kind replay lives in ``test_capture_replay.py``; select handling in
``test_capture_select.py``; cursor-trail annotations in
``test_capture_trail.py``. Shared fakes come from ``_capture_helpers.py``.
"""

from __future__ import annotations

import textwrap

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Step
from guidebot_recorder.scenario.loader import load_scenario

from ._capture_helpers import (
    FailingRecorder,
    FakePage,
    FakeRecorder,
    _async_none,
    _async_reason,
    _Boom,
    _cfg,
    _click_scenario_and_action,
    _compiled,
    _fp,
    _target,
)

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


class _RecordingPause:
    """Stand-in for `pause_for_inspection` that records its call arguments."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, page, phase, index, kind, exc, sensitive_values=(), **location):
        self.calls.append((page, phase, index, kind, exc, sensitive_values))
        self.location = location


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
