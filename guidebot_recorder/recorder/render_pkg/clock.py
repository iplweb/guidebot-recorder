"""Zegar renderu: kiedy narracja gra i gdzie zamrażamy obraz.

Umieszczenia audio zbierane są w KLATKACH osi nagrania, nie w sekundach —
kwantyzacja w momencie obserwacji jest tym, co utrzymuje monotoniczność
względem zamrożeń.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path

from guidebot_recorder.models.scenario import Step
from guidebot_recorder.recorder.render_pkg.errors import RenderError
from guidebot_recorder.tts.base import Segment
from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    apply_time_edits,
    probe_frame_count,
    seconds_to_frames,
)


def _narration(step: Step) -> str | None:
    return step.narration()


def _stamp_frame(anchor: float, *, not_before: int = 0) -> int:
    """Stamp "now" as a recording frame index, never earlier than *not_before*.

    Every audio placement is a wall-clock reading quantised onto the 25fps grid,
    and every such reading is later mapped through :meth:`Timeline.to_virtual`,
    which shifts a stamp past a freeze only when the freeze sits STRICTLY before
    it — a stamp exactly AT its own freeze point must stay put, because that is
    where the narration the freeze exists for begins.

    That rule is right, but it makes the grid unforgiving: a freeze recorded at
    frame ``F`` and a later event whose reading also rounds to ``F`` (the work
    in between took less than 40ms — a couple of CDP round-trips easily fits)
    are indistinguishable, so the later event maps to the START of the hold and
    fires up to a whole narration early. Nothing bounds that gap: ``settle``
    separates the step's own start from ``F``, not ``F`` from what follows it.

    So stamps are made monotonic against the freezes already emitted: once a
    freeze exists at ``F``, everything stamped afterwards is at least ``F + 1``
    and therefore lands after the hold. The cost is at most one frame (40ms) of
    placement error on an event that genuinely happened within the same frame,
    which is below the resolution the axis can represent at all.
    """
    return max(seconds_to_frames(time.monotonic() - anchor), not_before)


async def _pace_narration(
    segments: list[Segment],
    *,
    anchor: float,
    hold_frame: bool,
    settle: float,
    edits: list[TimeEdit],
    not_before: int = 0,
) -> int | None:
    """Pace one shared visual step by its longest configured narration.

    With ``hold_frame`` the wall clock only pays ``settle`` seconds — enough for
    entry animations triggered by this step to finish — and the rest of the
    voice-over becomes a held frame inserted in post. The settle comes *out of*
    the narration, not on top of it, so the finished film keeps the exact pacing
    it had when the renderer slept through the whole thing.

    Returns the recording frame the freeze was stamped at, or ``None`` when no
    freeze was recorded. *not_before* is the earliest frame this freeze may be
    stamped at — see :func:`_stamp_frame`; freezes are stamped through the same
    monotonic rule as everything else so a later freeze can never precede an
    earlier one on the recording axis.
    """

    if not segments:
        return None
    duration = max(segment.duration for segment in segments)

    if not hold_frame:
        await asyncio.sleep(duration)
        return None

    real = min(settle, duration)
    await asyncio.sleep(real)

    remaining = duration - real
    if remaining <= 0:
        return None
    at = _stamp_frame(anchor, not_before=not_before)
    edits.append(TimeEdit(at=at, kind="freeze", frames=seconds_to_frames(remaining)))
    return at


def _build_timeline(edits: Iterable[TimeEdit], *, source_frames: int) -> Timeline:
    """Coalesce collected edits onto the frame grid, then validate them.

    ``Timeline`` is deliberately strict — it rejects two edits on one frame, and
    anything at or past the end of the recording — because those are nonsense as
    a *model*. They are not nonsense as *observations*: a freeze recorded near
    the end can round past the 0.1s postroll and land at or beyond the last
    frame, and the clamp that pulls it back can then collide with a freeze
    already sitting there. Either would otherwise blow up after the entire
    recording is finished, losing the render.

    (``_stamp_frame`` now keeps freezes at least a frame apart as they are
    emitted, so two *unclamped* freezes can no longer share a frame. The merge
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
