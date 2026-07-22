"""From observed freezes to a validated model, and from the model to the file.

:func:`_build_timeline` reconciles what the run *observed* — freezes stamped by
:func:`~guidebot_recorder.recorder.render.narration._stamp_frame` — into something
``Timeline`` will accept as a *model*. :func:`_apply_timeline_edits` is then the
one place that model meets the file it describes, and checks the two against each
other exactly, in integer frames.

Three test seams land on this module. ``apply_time_edits`` and
``probe_frame_count`` are defined outside the package and name-imported here on
purpose: this is the module whose globals the consumer reads, so this is where the
patch belongs. ``probe_frame_count`` has a second consumer in
:mod:`~guidebot_recorder.recorder.render._run`, so replacing it takes **two** patch
lines. ``_apply_timeline_edits`` is the third — defined here, called through this
module object from ``_run``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    apply_time_edits,
    probe_frame_count,
)

from .errors import RenderError


def _build_timeline(edits: Iterable[TimeEdit], *, source_frames: int) -> Timeline:
    """Coalesce collected edits onto the frame grid, then validate them.

    ``Timeline`` is deliberately strict — it rejects two edits on one frame, and
    anything at or past the end of the recording — because those are nonsense as
    a *model*. They are not nonsense as *observations*: a freeze recorded near
    the end can round past the 0.1s postroll and land at or beyond the last
    frame, and the clamp that pulls it back can then collide with a freeze
    already sitting there. Either would otherwise blow up after the entire
    recording is finished, losing the render.

    (``narration._stamp_frame`` now keeps freezes at least a frame apart as they
    are emitted, so two *unclamped* freezes can no longer share a frame. The merge
    still has to exist for the clamped case, and is kept general rather than
    special-cased to it.)

    So the collected list is reconciled here, where the observations are:

    * an ``at`` at or beyond the end clamps to the last real frame — there is no
      later frame to hold, and the film must still gain those frames;
    * freezes sharing a frame merge by SUMMING their lengths, because two steps
      that both want the picture held at frame N mean the film holds frame N for
      the total of both. The film comes out exactly as long as the narration
      asked for, which is the invariant that matters.
    """
    merged: dict[int, TimeEdit] = {}
    passthrough: list[TimeEdit] = []
    for edit in edits:
        if edit.kind != "freeze":
            passthrough.append(edit)
            continue
        at = min(edit.at, source_frames - 1)
        previous = merged.get(at)
        frames = edit.frames + (previous.frames if previous else 0)
        merged[at] = TimeEdit(at=at, kind="freeze", frames=frames)
    return Timeline.build([*merged.values(), *passthrough], source_frames=source_frames)


def _apply_timeline_edits(source: Path, timeline: Timeline, dest: Path) -> None:
    """Apply *timeline* to *source*, then verify the result against the model.

    Everything downstream trusts ``Timeline.virtual_duration``: the audio beds
    are built to that length and ``mux_audio_tracks`` is handed the same number
    as its ``video_duration``, so its duration guard compares the model against
    itself and can never catch a model/file disagreement. This is the one place
    the model meets the file, so the check is exact — both sides are integer
    frame counts, and a difference of even one frame means the filtergraph did
    something other than what was modelled.
    """
    apply_time_edits(source, timeline, dest)
    produced = probe_frame_count(dest)
    if produced != timeline.virtual_frames:
        raise RenderError(
            f"time-edit stage produced {produced} frames but the timeline models "
            f"{timeline.virtual_frames} — audio would be written at the wrong length"
        )
