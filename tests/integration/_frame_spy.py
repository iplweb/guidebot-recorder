"""Recording the outlines a guide run paints into the page.

The target outline used to be an :class:`Annotation`, so an integration test
could read it straight off the finished ``GuidePage``. It is now drawn into the
live page before the shutter — that is the only way to get it *beneath* the
cursor, since a screenshot has no layers — and so it leaves no trace in the
returned pages at all.

These tests still have to check the same thing they always did: that the box the
guide outlines is the real control's box, measured in a real browser. So instead
of reading an annotation they record the calls, and assert on those.

The spy **calls through** to the real :meth:`Overlay.frame`. The outline must
still be painted: several of these tests go on to read pixels out of the
screenshot, and a spy that swallowed the call would quietly change the picture
they inspect.

Explicitly imported, like every other helper here — ``tests/`` has no
``conftest.py`` by design (decision D4).
"""

from __future__ import annotations

from dataclasses import dataclass

from guidebot_recorder.overlay.overlay import Overlay


@dataclass(frozen=True, slots=True)
class FramedBox:
    """One outline the run painted, in viewport pixels."""

    x: float
    y: float
    w: float
    h: float


def record_frames(monkeypatch) -> list[FramedBox]:
    """Patch :meth:`Overlay.frame` to append to the returned list, then call through.

    Patched on the class rather than on an instance because the run under test
    builds its own ``Overlay`` (sometimes more than one — a popup gets its own
    ``Recorder``), and the test never holds a reference to it.
    """

    painted: list[FramedBox] = []
    original = Overlay.frame

    async def spy(self, page, x, y, width, height):
        painted.append(FramedBox(x, y, width, height))
        await original(self, page, x, y, width, height)

    monkeypatch.setattr(Overlay, "frame", spy)
    return painted
