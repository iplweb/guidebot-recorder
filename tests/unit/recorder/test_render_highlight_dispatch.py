"""Dyspozytor `render` dla `highlight` — przypięty, bo jego brak jest cichy.

Łańcuch `elif` w ``_render_step`` nie ma `else`: usunięcie gałęzi `highlight`
daje krok bez animacji i **bez błędu**. Reszta suite'u tego nie wykryje, więc
kontrakt „scenariusz mówi highlight → rekorder dostaje wywołanie z pokrętłami
scalonymi z configiem" żyje tutaj.
"""

from __future__ import annotations

import pytest

from guidebot_recorder.models.config import Config, HighlightConfig, TtsConfig, Viewport
from guidebot_recorder.models.scenario import ResolvedHighlight, Scenario, Step
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.render import RenderError, _render_step
from guidebot_recorder.resolver.resolution import ResolvedTarget


class _FakePage:
    url = "https://example.test/raport"

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, script, arg=None):
        return {"cursor": True, "chrome": True, "mounted": True}


class _FakeOverlay:
    pos = (0.0, 0.0)

    async def ensure(self, page) -> None:
        pass


class _FakeRecorder:
    """Zapisuje, z czym wywołano `highlight`."""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.calls: list[ResolvedHighlight] = []

    async def highlight(self, target, spec: ResolvedHighlight) -> None:
        self.calls.append(spec)

    async def apply_readiness(self, expect) -> None:
        pass


def _config(**highlight) -> Config:
    return Config(
        title="Raport",
        viewport=Viewport(width=800, height=600),
        tts=TtsConfig(provider="fake", voice="v", lang="pl-PL"),
        highlight=HighlightConfig(**highlight),
    )


def _resolved() -> ResolvedTarget:
    return ResolvedTarget(
        action="highlight",
        target=RoleTarget(role="table", name="Wyniki", exact=True),
        locator=object(),
        input_text=None,
        state=None,
        identity=None,
    )


async def _noop_ensure_card(page) -> None:
    pass


async def _run(step: Step, cfg: Config) -> _FakeRecorder:
    page = _FakePage()
    recorder = _FakeRecorder(page)
    await _render_step(
        page,
        recorder,
        _FakeOverlay(),
        None,
        Scenario(config=cfg, steps=[step]),
        step,
        "highlight",
        3,
        None,
        0.0,
        {},
        _noop_ensure_card,
        resolved=_resolved(),
    )
    return recorder


async def test_render_dispatches_a_highlight_step_to_the_recorder():
    recorder = await _run(Step.model_validate({"highlight": "tabela z wynikami"}), _config())

    assert len(recorder.calls) == 1
    assert recorder.calls[0].what == "tabela z wynikami"


async def test_knobs_reach_the_recorder_merged_with_the_config():
    step = Step.model_validate({"highlight": {"what": "tabela", "loops": 4, "color": "#000"}})

    recorder = await _run(step, _config(padding=20, loops=2, hold=1.5, color="#fff"))

    spec = recorder.calls[0]
    assert (spec.loops, spec.color) == (4, "#000")  # krok wygrywa
    assert (spec.padding, spec.hold) == (20, 1.5)  # reszta z configu


async def test_a_sidecar_claiming_highlight_on_another_step_fails_loudly():
    """Rozjazd sidecara z plikiem nie może skończyć się cichym pominięciem."""

    with pytest.raises(RenderError, match="compile --force"):
        await _run(Step.model_validate({"click": "przycisk"}), _config())
