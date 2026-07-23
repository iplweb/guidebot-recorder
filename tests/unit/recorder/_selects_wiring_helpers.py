"""Shared scaffolding for the ``test_selects_wiring_*.py`` group.

This repo deliberately forgoes ``conftest.py``, so the fakes, builders and
fixture *factories* the split wiring tests share live here and are imported
explicitly. Two rules shape the shape of this file:

* A fixture whose name is also a test parameter cannot be shared by import
  (it would shadow the parameter and trip ``F811``). So ``browser`` and
  ``installs`` are exported as *factories* — :func:`browser_instance` and
  :func:`record_installs` — and each test module defines its own one-line
  fixture that calls them.
* Everything else (constants, plain builders, fake doubles) is an ordinary
  importable symbol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, async_playwright

from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Select, Step
from guidebot_recorder.models.target import LabelTarget, RoleTarget
from guidebot_recorder.recorder.recorder import SelectDriveError
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.resolution import ResolvedTarget
from guidebot_recorder.selects import Selects, SelectsNotReadyError

# charset is explicit: without it Chromium decodes a data: URL as latin-1 and the
# Polish label no longer matches.
SELECT_PAGE = (
    "data:text/html;charset=utf-8,<label for=woj>Województwo</label>"
    "<select id=woj><option>Mazowieckie</option><option>Śląskie</option></select>"
)


def _scenario_yaml(*, selects_block: str = "") -> str:
    """A two-step scenario; ``selects_block`` is an indented ``config`` entry."""

    return (
        "config:\n"
        "  title: Wybór\n"
        "  viewport: {width: 640, height: 480}\n"
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n"
        f"{selects_block}"
        "steps:\n"
        f'  - navigate: "{SELECT_PAGE}"\n'
        '  - teach: "kliknij Województwo"\n'
    )


class _MockReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="click",
            target=LabelTarget(label="Województwo", exact=True),
        )


def _config(**kwargs) -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=640, height=480),
        tts=TtsConfig(provider="fake", voice="v", lang="pl-PL"),
        **kwargs,
    )


@asynccontextmanager
async def browser_instance() -> AsyncIterator[Browser]:
    """Factory behind each module's ``browser`` fixture (see module docstring)."""

    async with async_playwright() as pw:
        instance = await pw.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            await instance.close()


def record_installs(monkeypatch) -> list[object]:
    """Factory behind each module's ``installs`` fixture.

    Records every context the widget is installed on, still installing it.
    """

    recorded: list[object] = []
    original = Selects.install_context

    async def spy(self, context):
        recorded.append(context)
        return await original(self, context)

    monkeypatch.setattr(Selects, "install_context", spy)
    return recorded


class _FakePage:
    url = "https://example.test/form"

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, script, arg=None):
        return {"cursor": True, "chrome": True, "mounted": True}


class _FakeOverlay:
    pos = (0.0, 0.0)

    async def ensure(self, page) -> None:  # pragma: no cover - mounted page never repairs
        pass


class _FakeRecorder:
    """Records how ``select`` was dispatched; optionally refuses to drive."""

    def __init__(self, page: _FakePage, *, fail: bool = False) -> None:
        self.page = page
        self.fail = fail
        #: raise the readiness barrier's failure instead of the drive failure
        self.not_ready = False
        self.calls: list[tuple[str, bool]] = []

    async def select(self, target, option: str, *, native: bool = False) -> None:
        self.calls.append((option, native))
        if self.not_ready:
            raise SelectsNotReadyError(
                "widget select nie zgłosił gotowości w ciągu 15.0 s dla ramki about:blank"
            )
        if self.fail:
            raise SelectDriveError("nie udało się wysterować widgetu 'Województwo'")

    async def apply_readiness(self, expect) -> None:
        pass


def _select_step(mode: str | None = None) -> Step:
    payload = {"from": "Województwo", "option": "Mazowieckie"}
    if mode is not None:
        payload["mode"] = mode
    return Step(select=Select(**payload))


def _select_scenario(step: Step, cfg: Config) -> Scenario:
    return Scenario(config=cfg, steps=[step])


def _resolved_select() -> ResolvedTarget:
    return ResolvedTarget(
        action="select",
        target=RoleTarget(role="combobox", name="Województwo", exact=True),
        locator=object(),
        input_text=None,
        state=None,
        identity=None,
    )


async def _noop_ensure_card(page) -> None:
    pass
