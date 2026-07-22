"""The narration clock: synthesis up front, pacing and stamping during the run.

Phase 0 (:func:`_presynthesize_narration`) turns every configured track into cache
entries before a browser exists, so a TTS failure can never land mid-recording.
During the run :func:`_pace_narration` spends the voice-over — as real wall clock,
or as a freeze inserted in post — and :func:`_stamp_frame` is the single rule that
quantises "now" onto the 25fps recording grid and keeps those stamps monotonic
against the freezes already emitted.

The freezes this module *observes* are reconciled into a model by
:mod:`~guidebot_recorder.recorder.render.timeline`. The two are kept apart because
this side observes and that side models.

``_pace_narration`` is a test seam: defined here, called through this module object
from :mod:`~guidebot_recorder.recorder.render._run`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    TtsProvider,
    cache_key,
)
from guidebot_recorder.video.timeline import TimeEdit, seconds_to_frames

_TTS_CONCURRENCY = 8


@dataclass(slots=True)
class _TtsWork:
    text: str
    config: TtsConfig
    destinations: list[tuple[str, int]]


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


async def _presynthesize_narration(
    steps: Sequence[Step],
    configs: list[TtsConfig],
    cache: TtsCache,
    provider: TtsProvider,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, dict[int, Segment]]:
    """Synthesize unique cache entries concurrently and map them to every step.

    Repeated narration can resolve to the same on-disk cache path. Deduplicating
    by the canonical cache key before scheduling prevents concurrent writers to
    that path and avoids duplicate provider calls on a cold cache.
    """

    segments: dict[str, dict[int, Segment]] = {tts.lang: {} for tts in configs}
    by_key: dict[str, _TtsWork] = {}
    for index, step in enumerate(steps):
        canonical_text = _narration(step)
        if canonical_text is None:
            continue
        for track_index, tts in enumerate(configs):
            text = canonical_text if track_index == 0 else step.translations[tts.lang]
            key = cache_key(
                text,
                tts,
                provider.adapter_version,
                CACHE_SCHEMA_VERSION,
            )
            work = by_key.get(key)
            if work is None:
                work = _TtsWork(text=text, config=tts, destinations=[])
                by_key[key] = work
            work.destinations.append((tts.lang, index))

    semaphore = asyncio.Semaphore(_TTS_CONCURRENCY)

    async def synthesize(work: _TtsWork) -> None:
        async with semaphore:
            segment = await cache.get_or_synth(work.text, work.config, provider)
        for language, index in work.destinations:
            segments[language][index] = segment
        if on_progress is not None:
            on_progress(len(work.destinations))

    results = await asyncio.gather(
        *(synthesize(work) for work in by_key.values()),
        return_exceptions=True,
    )
    # Wait for every started cache writer before propagating an error; otherwise
    # a sibling task could keep writing after Phase 0 has already returned.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return segments
