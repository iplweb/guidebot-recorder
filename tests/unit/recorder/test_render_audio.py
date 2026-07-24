"""``recorder.render.audio``: assembling, publishing and muxing the audio tracks.

Split out of the original ``test_render.py``. Sibling ``test_render_sound.py``
covers the SFX bed; the language-track probes here (``_stream_types`` etc.) are
private to this file.
"""

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import RenderError, _mux_tracks_for_timeline, run_render
from guidebot_recorder.recorder.render.audio import (
    _assemble_audio_tracks,
    _publish_render_artifacts,
)
from guidebot_recorder.recorder.render.narration import _pace_narration
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled
from guidebot_recorder.tts.base import Segment
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import MuxAudioTrack
from guidebot_recorder.video.mux.probe import probe_duration

from ._render_helpers import FFMPEG, SCENARIO, FakeTts, MockReasoner

pytestmark = FFMPEG


def _stream_types(path: Path) -> list[str]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _decode_audio_stream(path: Path, index: int) -> bytes:
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:a:{index}",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout


def _audio_codec_details(path: Path) -> dict:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output)["streams"][0]


class MultilingualFakeTts(FakeTts):
    durations = {
        ("Pierwszy krok.", "pl-PL"): 0.7,
        ("First step.", "en-US"): 0.2,
        ("Drugi krok.", "pl-PL"): 0.1,
        ("Second step.", "en-US"): 0.4,
    }
    frequencies = {"pl-PL": 440, "en-US": 880}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        duration = self.durations[(text, tts.lang)]
        self.calls.append((text, tts.lang))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={self.frequencies[tts.lang]}:"
                f"duration={duration}:sample_rate=48000",
                "-t",
                str(duration),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return duration


class FailingSecondTrackTts:
    adapter_version = 1

    def __init__(self) -> None:
        self.calls = 0

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("alternate synthesis failed")
        out.write_bytes(b"first track")
        return 0.1


async def test_render_produces_mp4_with_audio(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1


async def test_render_produces_one_video_with_multiple_language_tracks(tmp_path, monkeypatch):
    path = tmp_path / "multilingual.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Wielojęzyczny film
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v-pl, lang: pl-PL, title: Polski, trackLanguage: pol}
              audioTracks:
                - {provider: fake, voice: v-en, lang: en-US, title: English, trackLanguage: eng}
            steps:
              - say: "Pierwszy krok."
                translations: {en-US: "First step."}
              - say: "Drugi krok."
                translations: {en-US: "Second step."}
            """
        ),
        encoding="utf-8",
    )
    provider = MultilingualFakeTts()
    narration_waits: list[float] = []

    async def observe_narration_wait(segments: list[Segment], **kwargs) -> None:
        narration_waits.append(max(segment.duration for segment in segments))
        await _pace_narration(segments, **kwargs)

    monkeypatch.setattr(
        "guidebot_recorder.recorder.render.narration._pace_narration",
        observe_narration_wait,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, provider, tmp_path / "cache", browser)
        second_out = tmp_path / "second.mp4"
        await run_render(path, second_out, provider, tmp_path / "cache", browser)
        await browser.close()

    assert provider.calls == [
        ("Pierwszy krok.", "pl-PL"),
        ("First step.", "en-US"),
        ("Drugi krok.", "pl-PL"),
        ("Second step.", "en-US"),
    ]
    assert narration_waits == [0.7, 0.4, 0.7, 0.4]
    assert _stream_types(out) == ["video", "audio", "audio"]
    details = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream_tags=language,title,handler_name:stream_disposition=default",
                "-of",
                "json",
                str(out),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["streams"]
    assert [stream["tags"]["language"] for stream in details] == ["pol", "eng"]
    assert [stream["tags"]["handler_name"] for stream in details] == ["Polski", "English"]
    assert [stream["disposition"]["default"] for stream in details] == [1, 0]
    out_work = tmp_path / ".guidebot_video" / "out"
    second_work = tmp_path / ".guidebot_video" / "second"
    assert (out_work / "bed-pol.wav").exists()
    assert (out_work / "bed-eng.wav").exists()
    assert (second_work / "bed-pol.wav").exists()
    assert (second_work / "bed-eng.wav").exists()
    assert (out_work / "bed-pol.wav").read_bytes() != (out_work / "bed-eng.wav").read_bytes()
    assert _decode_audio_stream(out, 0) != _decode_audio_stream(out, 1)
    for bed in (out_work / "bed-pol.wav", out_work / "bed-eng.wav"):
        assert _audio_codec_details(bed) == {
            "codec_name": "pcm_s16le",
            "sample_rate": "48000",
            "channels": 2,
        }
        assert probe_duration(bed) == pytest.approx(probe_duration(out), abs=0.05)
    assert probe_duration(out) >= 1.1


async def test_render_rejects_multiple_tts_providers_before_recording(tmp_path):
    path = tmp_path / "mixed-providers.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Mixed providers
              viewport: {width: 640, height: 480}
              tts: {provider: edge, voice: pl, lang: pl-PL, trackLanguage: pol}
              audioTracks:
                - {provider: other, voice: en, lang: en-US, trackLanguage: eng}
            steps:
              - say: "Witaj."
                translations: {en-US: "Welcome."}
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.mp4"

    with pytest.raises(RenderError, match="jeden provider TTS"):
        await run_render(
            path,
            out,
            FakeTts(),
            tmp_path / "cache",
            object(),  # type: ignore[arg-type] -- must not be touched before the failure
        )

    assert not out.exists()


async def test_render_aborts_on_alternate_synthesis_failure_before_recording(tmp_path):
    path = tmp_path / "failing-tts.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Failing alternate TTS
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: pl, lang: pl-PL, trackLanguage: pol}
              audioTracks:
                - {provider: fake, voice: en, lang: en-US, trackLanguage: eng}
            steps:
              - say: "Witaj."
                translations: {en-US: "Welcome."}
            """
        ),
        encoding="utf-8",
    )
    write_compiled(
        compiled_path(path),
        CompiledScenario(source=path.name, actions=[None]),
    )
    provider = FailingSecondTrackTts()
    out = tmp_path / "out.mp4"

    with pytest.raises(RuntimeError, match="alternate synthesis failed"):
        await run_render(
            path,
            out,
            provider,
            tmp_path / "cache",
            object(),  # type: ignore[arg-type] -- Phase 0 must fail before browser use
        )

    assert provider.calls == 2
    assert not out.exists()
    assert not (tmp_path / ".guidebot_video" / "out").exists()


async def test_audio_timeline_rejects_even_subframe_narration_overrun(tmp_path):
    tts = TtsConfig(
        provider="fake",
        voice="pl",
        lang="pl-PL",
        trackLanguage="pol",
    )
    segment = Segment(text="koniec", path=tmp_path / "unused.mp3", duration=0.08)

    with pytest.raises(RenderError, match="wykracza poza nagranie"):
        await _mux_tracks_for_timeline(
            [tts],
            {"pl-PL": [Placed(segment=segment, offset=0.95)]},
            total=1.0,
            work=tmp_path,
        )


def test_publish_render_artifacts_replaces_complete_set_and_removes_stale_languages(tmp_path):
    work = tmp_path / "work"
    staging = tmp_path / "staging"
    out = tmp_path / "out.mp4"
    work.mkdir()
    staging.mkdir()
    out.write_bytes(b"old master")
    (work / "bed-pol.wav").write_bytes(b"old Polish")
    (work / "bed-eng.wav").write_bytes(b"obsolete English")
    (work / "recording.webm").write_bytes(b"keep unrelated artifacts")
    new_polish = staging / "bed-pol.wav"
    new_polish.write_bytes(b"new Polish")
    staged_master = staging / "out.mp4"
    staged_master.write_bytes(b"new master")

    _publish_render_artifacts(
        staged_master,
        [MuxAudioTrack(new_polish, language="pol", default=True)],
        work,
        out,
    )

    assert out.read_bytes() == b"new master"
    assert (work / "bed-pol.wav").read_bytes() == b"new Polish"
    assert not (work / "bed-eng.wav").exists()
    assert (work / "recording.webm").exists()


def test_publish_render_artifacts_rolls_back_keyboard_interrupt(tmp_path, monkeypatch):
    work = tmp_path / "work"
    staging = tmp_path / "staging"
    out = tmp_path / "out.mp4"
    work.mkdir()
    staging.mkdir()
    out.write_bytes(b"old master")
    (work / "bed-pol.wav").write_bytes(b"old Polish")
    (work / "bed-eng.wav").write_bytes(b"old English")
    new_polish = staging / "bed-pol.wav"
    new_polish.write_bytes(b"new Polish")
    staged_master = staging / "out.mp4"
    staged_master.write_bytes(b"new master")
    real_replace = os.replace

    def interrupt_at_master(source, destination):
        if Path(source) == staged_master and Path(destination) == out:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr("guidebot_recorder.recorder.render.audio.os.replace", interrupt_at_master)

    with pytest.raises(KeyboardInterrupt):
        _publish_render_artifacts(
            staged_master,
            [MuxAudioTrack(new_polish, language="pol", default=True)],
            work,
            out,
        )

    assert out.read_bytes() == b"old master"
    assert (work / "bed-pol.wav").read_bytes() == b"old Polish"
    assert (work / "bed-eng.wav").read_bytes() == b"old English"
    assert not list(work.glob(".audio-beds-backup-*"))


@pytest.mark.parametrize("failure_point", ["second_bed", "mux"])
async def test_assemble_failure_preserves_previous_master_and_complete_bed_set(
    tmp_path, monkeypatch, failure_point
):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out.mp4"
    out.write_bytes(b"old master")
    (work / "bed-pol.wav").write_bytes(b"old Polish")
    (work / "bed-eng.wav").write_bytes(b"old English")
    configs = [
        TtsConfig(provider="fake", voice="pl", lang="pl-PL", trackLanguage="pol"),
        TtsConfig(provider="fake", voice="en", lang="en-US", trackLanguage="eng"),
    ]
    build_calls = 0

    def staged_bed(placed, total, destination):
        nonlocal build_calls
        build_calls += 1
        if failure_point == "second_bed" and build_calls == 2:
            raise RuntimeError("second bed failed")
        destination.write_bytes(f"new bed {build_calls}".encode())

    def staged_mux(video, tracks, destination, *, preencoded=False, video_duration=None, fade=None):
        assert video_duration == 1.0
        if failure_point == "mux":
            raise RuntimeError("mux failed")
        destination.write_bytes(b"new master")

    monkeypatch.setattr("guidebot_recorder.recorder.render.audio.build_audio_bed", staged_bed)
    monkeypatch.setattr("guidebot_recorder.recorder.render.audio.mux_audio_tracks", staged_mux)

    with pytest.raises(RuntimeError, match="failed"):
        await _assemble_audio_tracks(
            tmp_path / "video.webm",
            configs,
            {"pl-PL": [], "en-US": []},
            1.0,
            work,
            out,
        )

    assert out.read_bytes() == b"old master"
    assert (work / "bed-pol.wav").read_bytes() == b"old Polish"
    assert (work / "bed-eng.wav").read_bytes() == b"old English"
    assert not list(work.glob(".audio-beds-*"))
