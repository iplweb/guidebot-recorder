"""Shared scaffolding for the ``capture_pages`` unit tests.

The live-capture tests are split across ``test_capture_replay.py``,
``test_capture_select.py``, ``test_capture_errors.py`` and
``test_capture_trail.py``. Everything they hold in common lives here and is
imported explicitly — the repo runs without ``conftest.py`` on purpose.

Nothing in this module is a ``@pytest.fixture``; the fakes and factories are
imported and instantiated directly by each test file. The single-consumer
recorder subclasses (``UndrivableSelectRecorder`` and the two inline recorders)
stay next to their one test rather than travelling here.
"""

from __future__ import annotations

from pathlib import Path

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import Scenario, Step
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.recorder import (
    OPTION_MISSING,
    PointResult,
    SelectDriveError,
    SelectReveal,
)


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


class _Boom(RuntimeError):
    """A step failure that is neither PlaywrightError nor GuideError."""


class FailingRecorder(FakeRecorder):
    async def point(self, target, ripple=False):
        raise _Boom("krok padł na sekrecie hunter2")


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


def _click_scenario_and_action():
    scenario = Scenario(config=_cfg(), steps=[Step(click="przycisk zapisu")])
    action = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    return scenario, action


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
