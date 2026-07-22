"""Building the audio beds and publishing the finished artifacts.

One full-length bed per configured language, muxed onto the video and then
committed together with the master MP4. Publication is a two-phase commit around
``os.replace``: the existing beds are moved aside, the new ones are put in place,
and the master replace is the commit point — anything that fails rolls the
previous bed set back.

That ``os.replace`` is itself a test seam, patched as
``guidebot_recorder.recorder.render.audio.os.replace`` — through *this* module's
``os`` global, because this is the module that performs the rename. The path must
name this module for the same reason every other seam does: it is the globals the
consumer reads at call time.

The remaining seams are ``build_audio_bed``, ``build_sfx_bed`` and
``mux_audio_tracks`` (defined outside the package, name-imported here, patched on
this module) and ``_AUDIO_BED_CONCURRENCY``, ``_publish_render_artifacts`` and
``_assemble_audio_tracks``, which are defined here. The first two are read out of
this module's own globals; ``_assemble_audio_tracks`` is called through this module
object from :mod:`~guidebot_recorder.recorder.render._run`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from importlib.resources import as_file, files
from pathlib import Path

from guidebot_recorder.models.config import SoundConfig, TtsConfig
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import FadeSpec, MuxAudioTrack, mux_audio_tracks
from guidebot_recorder.video.sfx import build_sfx_bed, mix_sfx_into_bed

from .errors import RenderError

# Each worker can own a full ffmpeg process. Keep the pool below both the host's
# CPU count and a conservative process ceiling instead of scaling with languages.
_AUDIO_BED_CONCURRENCY = max(1, min(4, os.cpu_count() or 1))


def _assert_narration_fits(
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
) -> None:
    """No track may run past the picture. Checked before a single bed is built."""

    for tts in configs:
        for placement in placed_by_language[tts.lang]:
            if placement.offset + placement.segment.duration > total:
                raise RenderError(
                    f"narracja {tts.lang} wykracza poza nagranie wideo — render przerwany"
                )


async def _gather_tracks(tasks: list[asyncio.Task[MuxAudioTrack]]) -> list[MuxAudioTrack]:
    """Collect every bed, draining the workers if the caller is cancelled.

    Moved out of :func:`_mux_tracks_for_timeline` verbatim. Cancelling an asyncio
    wrapper cannot stop a running thread or its ffmpeg child, and the staging
    ``TemporaryDirectory`` is unwound by the caller the moment this returns — so
    the shield/drain below is what keeps ffmpeg from writing into a deleted path.
    Do not tidy it.
    """

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

    _assert_narration_fits(configs, placed_by_language, total)

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
    return await _gather_tracks(tasks)


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
