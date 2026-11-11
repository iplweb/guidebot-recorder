"""Faza 0 (pre-synteza narracji) i montaż ścieżek dźwiękowych na końcu renderu."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from importlib.resources import as_file, files
from collections.abc import Callable, Sequence
from pathlib import Path

from guidebot_recorder.models.config import SoundConfig, TtsConfig
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.recorder.render_pkg.clock import _narration
from guidebot_recorder.recorder.render_pkg.errors import RenderError
from guidebot_recorder.recorder.render_pkg.state import _TtsWork
from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    TtsProvider,
    cache_key,
)
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import FadeSpec, MuxAudioTrack, mux_audio_tracks
from guidebot_recorder.video.sfx import build_sfx_bed, mix_sfx_into_bed

_TTS_CONCURRENCY = 8
# Each worker can own a full ffmpeg process. Keep the pool below both the host's
# CPU count and a conservative process ceiling instead of scaling with languages.
_AUDIO_BED_CONCURRENCY = max(1, min(4, os.cpu_count() or 1))


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


async def _mux_tracks_for_timeline(
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    *,
    sfx_bed: Path | None = None,
) -> list[MuxAudioTrack]:
    """Build one full-length bed per language in deterministic stream order.

    When *sfx_bed* is set, narration is rendered to a temp name first, then the
    shared SFX bed is mixed into the final `bed-<lang>.wav` so ``bed-*.wav`` keeps
    naming ``_publish_render_artifacts`` relies on.
    """

    for tts in configs:
        for placement in placed_by_language[tts.lang]:
            if placement.offset + placement.segment.duration > total:
                raise RenderError(
                    f"narracja {tts.lang} wykracza poza nagranie wideo — render przerwany"
                )

    semaphore = asyncio.Semaphore(_AUDIO_BED_CONCURRENCY)

    def build_track(index: int, tts: TtsConfig) -> MuxAudioTrack:
        bed = work / f"bed-{tts.mp4_language()}.wav"
        if sfx_bed is not None:
            narr = work / f"narr-{tts.mp4_language()}.wav"
            build_audio_bed(placed_by_language[tts.lang], total, narr)
            mix_sfx_into_bed(narr, sfx_bed, bed, total)  # bed = narration + SFX
        else:
            build_audio_bed(placed_by_language[tts.lang], total, bed)
        return MuxAudioTrack(
            path=bed,
            language=tts.mp4_language(),
            title=tts.title or tts.lang,
            default=index == 0,
        )

    async def build_bounded(index: int, tts: TtsConfig) -> MuxAudioTrack:
        async with semaphore:
            worker = asyncio.create_task(asyncio.to_thread(build_track, index, tts))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an asyncio wrapper cannot stop a running thread (or
                # its ffmpeg child). Keep the staging directory alive until that
                # worker has actually returned, with caller cancellation primary.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                if not worker.cancelled():
                    try:
                        worker.result()
                    except BaseException:
                        pass
                raise

    tasks = [asyncio.create_task(build_bounded(index, tts)) for index, tts in enumerate(configs)]
    gathered = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(gathered)
    except asyncio.CancelledError:
        # Do not start queued ffmpeg work after cancellation, but let workers
        # already inside to_thread finish before TemporaryDirectory can unwind.
        for task in tasks:
            task.cancel()
        while not gathered.done():
            try:
                await asyncio.shield(gathered)
            except asyncio.CancelledError:
                continue
        if not gathered.cancelled():
            gathered.result()
        raise
    tracks: list[MuxAudioTrack] = []
    # gather preserves config order. It also waits for all ffmpeg workers before
    # an error leaves the staging directory, avoiding writes into deleted paths.
    for result in results:
        if isinstance(result, BaseException):
            raise result
        tracks.append(result)
    return tracks


def _publish_render_artifacts(
    staged_mp4: Path,
    tracks: list[MuxAudioTrack],
    work: Path,
    out_mp4: Path,
) -> None:
    """Commit the new master and complete bed set, rolling back publish errors."""

    backup = Path(tempfile.mkdtemp(prefix=".audio-beds-backup-", dir=work))
    published: list[Path] = []
    try:
        for current in list(work.glob("bed-*.wav")):
            os.replace(current, backup / current.name)
        for track in tracks:
            destination = work / track.path.name
            os.replace(track.path, destination)
            published.append(destination)
        # The master is the commit point: until this atomic replace succeeds, the
        # previous MP4 remains in place and any bed publication error is rolled back.
        os.replace(staged_mp4, out_mp4)
    except BaseException:
        for destination in published:
            destination.unlink(missing_ok=True)
        for previous in backup.glob("bed-*.wav"):
            os.replace(previous, work / previous.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


async def _assemble_audio_tracks(
    video: Path,
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    out_mp4: Path,
    *,
    preencoded: bool = False,
    sound: SoundConfig | None = None,
    sfx_offsets: list[tuple[str, float]] | None = None,
    fade: FadeSpec | None = None,
) -> None:
    """Stage a complete bed set, mux atomically, then publish durable WAVs.

    When *sound* is enabled and *sfx_offsets* is non-empty, the shared SFX bed is
    built ONCE in staging (from the packaged click/key assets) and mixed into every
    language's narration bed via `_mux_tracks_for_timeline`.
    """

    with tempfile.TemporaryDirectory(prefix=".audio-beds-", dir=work) as staging:
        staged_mp4 = Path(staging) / f"{out_mp4.stem}.mp4"
        sfx_bed = None
        if sound is not None and sound.enabled and sfx_offsets:
            sfx_bed = Path(staging) / "sfx-bed.wav"
            sfx_pkg = files("guidebot_recorder.sfx")
            with (
                as_file(sfx_pkg.joinpath("click.wav")) as cp,
                as_file(sfx_pkg.joinpath("key.wav")) as kp,
            ):
                build_sfx_bed(
                    sfx_offsets,
                    total,
                    sfx_bed,
                    click_path=Path(cp),
                    key_path=Path(kp),
                    gain_db=sound.volume,
                )
        tracks = await _mux_tracks_for_timeline(
            configs,
            placed_by_language,
            total,
            Path(staging),
            sfx_bed=sfx_bed,
        )
        mux_audio_tracks(
            video,
            tracks,
            staged_mp4,
            preencoded=preencoded,
            video_duration=total,
            fade=fade,
        )
        _publish_render_artifacts(staged_mp4, tracks, work, out_mp4)
