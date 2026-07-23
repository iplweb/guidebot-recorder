"""The recording axis: freezes, and everything placed against them.

The third of the three lifetimes ``run_render`` used to interleave. A
:class:`_Clock` starts when the first frame is captured and collects three things,
all in recording-axis FRAMES rather than seconds — the grid is what
:class:`~guidebot_recorder.video.timeline.Timeline` reasons on, and quantising once
at the moment of observation is what lets :func:`_stamp_frame` keep placements
monotonic against the freezes. Seconds reappear only at the very end, when the
audio bed is built.

**Why ``last_freeze_frame`` is a field and ``note_sfx`` is a bound method.**

``on_sfx`` is handed to :class:`~guidebot_recorder.recorder.recorder.Recorder` and
fires one call frame *down*, inside ``_render_step``, at whatever instant the
click or keystroke happens. It has to read ``last_freeze_frame`` **as of that
instant** — a freeze emitted by the narration of the same step is exactly what it
must be clamped past. ``run_render`` used to get that from a closure over a local;
passing the value into a step function instead would break it **silently**: every
length check still passes (``probe_frame_count == virtual_frames``, the mux
duration guard, the per-track overrun check), because a collapsed placement does
not change how long the film is. Only the *position* of sounds and voice-overs
moves.

A bound method is that closure, by construction: ``clock.note_sfx`` re-reads
``self`` at call time, so there is no value to pass and nothing to get stale.
Three tests assert the placements this protects —
``test_hold_frame_narrations_never_overlap``,
``test_sfx_after_a_freeze_never_lands_inside_the_hold`` (``test_render.py``) and
``test_hold_frame_narrations_inside_taken_branch_never_overlap``
(``test_render_optional.py``) — and their docstrings say why every other guard in
the suite stays green while offsets collapse.

``_pace_narration`` is a test seam and is called through the ``narration`` module
object, so a patch on its defining module reaches this call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guidebot_recorder.models.config import Config, SoundConfig, TtsConfig
from guidebot_recorder.tts.base import Segment
from guidebot_recorder.video.timeline import TimeEdit

from . import narration
from .errors import RenderError
from .narration import _stamp_frame


@dataclass
class _Clock:
    """The recording axis and everything placed on it. One reader, one writer."""

    anchor: float
    placed_by_language: dict[str, list[tuple[Segment, int]]]
    sfx_events: list[tuple[str, int]] = field(default_factory=list)
    time_edits: list[TimeEdit] = field(default_factory=list)
    """Freezes recorded while rendering, on the *recording* axis. Applied to the
    video — and used to remap every audio offset — once the loop is done."""
    last_freeze_frame: int = -1
    """Frame of the most recent freeze, or -1 before any.

    Read by :meth:`stamp` so nothing is ever stamped inside a hold that was
    already recorded. This class is its only reader and its only writer, which is
    what makes the monotonicity a property of the type rather than of a comment.
    """

    @classmethod
    def started(cls, anchor: float, audio_configs: list[TtsConfig]) -> _Clock:
        return cls(anchor=anchor, placed_by_language={tts.lang: [] for tts in audio_configs})

    def stamp(self) -> int:
        """"Now", as a recording frame, never inside a freeze already emitted."""

        return _stamp_frame(self.anchor, not_before=self.last_freeze_frame + 1)

    def note_sfx(self, kind: str) -> None:
        """``Recorder(on_sfx=...)``. A bound method on purpose — see the module docstring."""

        self.sfx_events.append((kind, self.stamp()))

    def place_narration(
        self, index: int, audio_configs: list[TtsConfig], segments: dict[str, dict[int, Segment]]
    ) -> tuple[list[Segment], int]:
        """Place step *index*'s narration on every track, at one shared frame.

        Returns the segments that were placed (empty when this step is silent) and
        the frame they were placed at — the caller passes it back as
        :meth:`pace`'s ``not_before``. The frame is taken once, before the tracks
        are visited, so every language starts at the same instant.
        """

        frame = self.stamp()
        placed: list[Segment] = []
        for tts in audio_configs:
            segment = segments[tts.lang].get(index)
            if segment is not None:
                self.placed_by_language[tts.lang].append((segment, frame))
                placed.append(segment)
        return placed, frame

    async def pace(self, segments: list[Segment], cfg: Config, *, not_before: int) -> None:
        """Spend the step's voice-over, recording the freeze it may have emitted.

        One picture timeline: the action waits for the longest language, while
        shorter tracks naturally contain silence before the action.
        """

        emitted = await narration._pace_narration(
            segments,
            anchor=self.anchor,
            hold_frame=cfg.hold_frame_for_narration,
            settle=cfg.hold_frame_settle,
            edits=self.time_edits,
            not_before=not_before,
        )
        if emitted is not None:
            self.last_freeze_frame = emitted

    def sfx_frames(self, sound: SoundConfig) -> list[tuple[str, int]]:
        """The SFX the configuration actually wants, on the recording axis."""

        if not sound.enabled:
            return []
        frames: list[tuple[str, int]] = []
        for kind, frame in self.sfx_events:
            if kind == "click" and not sound.click:
                continue
            if kind == "key" and not sound.keys:
                continue
            if frame < 0:
                raise RenderError(f"ujemna klatka SFX ({frame}) — błąd zegara renderu")
            frames.append((kind, frame))
        return frames
