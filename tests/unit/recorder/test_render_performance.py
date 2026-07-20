from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

import guidebot_recorder.recorder.render as render_module
from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.tts.base import TtsCache


class ConcurrencyProbeTts:
    adapter_version = 1

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        self.calls.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # Complete out of input order so the result mapping cannot rely on
            # task completion order.
            await asyncio.sleep(0.001 * (12 - int(text.removeprefix("tekst-"))))
            out.write_bytes(text.encode())
            return float(int(text.removeprefix("tekst-")))
        finally:
            self.active -= 1


async def test_presynthesis_is_bounded_deduplicated_and_keeps_step_mapping(tmp_path) -> None:
    texts = [*(f"tekst-{index}" for index in range(12)), "tekst-3", "tekst-3"]
    steps = [Step(say=text) for text in texts]
    tts = TtsConfig(provider="fake", voice="v", lang="pl-PL")
    provider = ConcurrencyProbeTts()
    progress: list[int] = []

    segments = await render_module._presynthesize_narration(
        steps,
        [tts],
        TtsCache(tmp_path / "cache"),
        provider,
        on_progress=progress.append,
    )

    assert provider.max_active == 8
    assert len(provider.calls) == 12
    assert provider.calls.count("tekst-3") == 1
    assert [segments[tts.lang][index].text for index in range(len(texts))] == texts
    assert sum(progress) == len(texts)


async def test_audio_beds_use_bounded_threads_and_keep_track_order(tmp_path, monkeypatch) -> None:
    configs = [
        TtsConfig(
            provider="fake",
            voice=f"voice-{index}",
            lang=f"lang-{index}",
            trackLanguage=f"x{index:02d}",
        )
        for index in range(6)
    ]
    placed = {tts.lang: [] for tts in configs}
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_build_audio_bed(_placed, _total, out: Path) -> None:
        nonlocal active, max_active
        index = int(out.stem.rsplit("x", 1)[1])
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # Deliberately reverse completion order.
            time.sleep(0.005 * (6 - index))
            out.write_bytes(str(index).encode())
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(render_module, "_AUDIO_BED_CONCURRENCY", 2)
    monkeypatch.setattr(render_module, "build_audio_bed", fake_build_audio_bed)

    tracks = await render_module._mux_tracks_for_timeline(
        configs,
        placed,
        total=1.0,
        work=tmp_path,
    )

    assert max_active == 2
    assert [track.language for track in tracks] == [f"x{index:02d}" for index in range(6)]
    assert [track.default for track in tracks] == [True, False, False, False, False, False]


async def test_audio_bed_cancellation_waits_for_running_thread(tmp_path, monkeypatch) -> None:
    tts = TtsConfig(
        provider="fake",
        voice="voice",
        lang="pl-PL",
        trackLanguage="pol",
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    staging_was_alive = False

    def blocking_build(_placed, _total, out: Path) -> None:
        nonlocal staging_was_alive
        started.set()
        release.wait(timeout=5)
        staging_was_alive = out.parent.exists()
        out.write_bytes(b"bed")
        finished.set()

    monkeypatch.setattr(render_module, "build_audio_bed", blocking_build)
    monkeypatch.setattr(render_module, "mux_audio_tracks", lambda *args, **kwargs: None)
    monkeypatch.setattr(render_module, "_publish_render_artifacts", lambda *args: None)

    work = tmp_path / "work"
    work.mkdir()
    task = asyncio.create_task(
        render_module._assemble_audio_tracks(
            tmp_path / "video.webm",
            [tts],
            {tts.lang: []},
            total=1.0,
            work=work,
            out_mp4=tmp_path / "out.mp4",
        )
    )
    while not started.is_set():
        await asyncio.sleep(0.001)

    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert staging_was_alive is True
    assert not list(work.glob(".audio-beds-*"))
