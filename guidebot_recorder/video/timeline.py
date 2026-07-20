"""Render time as data: an explicit, frame-exact map from recording to film.

Playwright records at a fixed 25 fps, so the whole model is expressed in whole
frames. Seconds only appear at the boundaries — wall-clock readings convert in,
audio offsets convert out. Nothing in between is a float, because ``tpad``
quantises to the frame grid and any float slack accumulates across freezes until
it trips the duration guard in :mod:`guidebot_recorder.video.mux`.

Two axes exist:

``t_real``
    the recording produced by Playwright.
``t_virtual``
    the finished film, after freezes are inserted and cuts removed.

:meth:`Timeline.to_virtual` is the only bridge between them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

FPS = 25
"""Frames per second of a Playwright screencast.

Hardcoded in Playwright's ``videoRecorder`` and not exposed through the API.
Verified: every inter-frame delta is exactly 0.04 s, even when the page's main
thread stalls (Chromium repeats the last frame rather than dropping the grid).
"""

EditKind = Literal["freeze", "cut"]


class TimelineError(ValueError):
    """A timeline that cannot be rendered — raised before ffmpeg is invoked."""


def seconds_to_frames(seconds: float) -> int:
    """Convert *seconds* to the nearest whole frame."""
    return int(round(seconds * FPS))


def frames_to_seconds(frames: int) -> float:
    """Convert a whole number of *frames* to seconds, exactly on the grid."""
    return frames / FPS


@dataclass(frozen=True)
class TimeEdit:
    """One edit applied to the recording axis.

    ``freeze``
        hold the frame at index *at* for *frames* additional frames.
    ``cut``
        drop frames ``[at, at + frames)`` from the recording.
    """

    at: int
    kind: EditKind
    frames: int


@dataclass(frozen=True)
class Timeline:
    """An ordered, validated set of edits over a recording of known length."""

    edits: tuple[TimeEdit, ...]
    source_frames: int

    @classmethod
    def build(cls, edits: Iterable[TimeEdit], source_frames: int) -> Timeline:
        """Validate and order *edits*, failing loud on anything unrenderable."""
        if source_frames <= 0:
            raise TimelineError(f"source_frames must be positive, got {source_frames}")

        ordered = tuple(sorted(edits, key=lambda e: e.at))

        for edit in ordered:
            if edit.at < 0:
                raise TimelineError(f"edit position must be >= 0, got {edit.at}")
            if edit.frames <= 0:
                raise TimelineError(f"edit length must be positive, got {edit.frames}")
            if edit.at >= source_frames:
                raise TimelineError(
                    f"edit at frame {edit.at} is beyond the recording ({source_frames} frames)"
                )
            if edit.kind == "cut" and edit.at + edit.frames > source_frames:
                raise TimelineError(
                    f"cut [{edit.at}, {edit.at + edit.frames}) overruns "
                    f"the recording ({source_frames} frames)"
                )

        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.at == previous.at:
                raise TimelineError(f"two edits share frame {current.at}")
            if previous.kind == "cut" and current.at < previous.at + previous.frames:
                raise TimelineError(
                    f"edit at frame {current.at} falls inside the cut "
                    f"[{previous.at}, {previous.at + previous.frames})"
                )

        return cls(edits=ordered, source_frames=source_frames)

    @property
    def is_empty(self) -> bool:
        """Whether this timeline leaves the recording untouched."""
        return not self.edits

    def to_virtual(self, frame: int) -> int:
        """Map a recording frame index onto the finished film.

        A frame inside a cut span clamps to that span's start: cuts remove dead
        time, so nothing meaningful is lost, but the choice is deliberate rather
        than an accident of arithmetic.
        """
        shift = 0
        for edit in self.edits:
            if edit.kind == "freeze":
                if edit.at < frame:
                    shift += edit.frames
            else:
                if edit.at + edit.frames <= frame:
                    shift -= edit.frames
                elif edit.at < frame:
                    return edit.at + shift
        return frame + shift

    def to_virtual_seconds(self, t_real: float) -> float:
        """Map a wall-clock offset (seconds) onto the finished film."""
        return frames_to_seconds(self.to_virtual(seconds_to_frames(t_real)))

    @property
    def virtual_frames(self) -> int:
        """Length of the finished film, in frames."""
        return self.to_virtual(self.source_frames)

    @property
    def virtual_duration(self) -> float:
        """Length of the finished film, in seconds."""
        return frames_to_seconds(self.virtual_frames)
