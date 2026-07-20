import asyncio
import json
import os
import shutil
import subprocess
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import ChromeConfig, CursorConfig, TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder import render as render_module
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import (
    _POPUP_REQUEST_SCRIPT,
    POPUP_BBOX_DEGENERATE_RATIO,
    RenderError,
    _assemble_audio_tracks,
    _mux_tracks_for_timeline,
    _parse_content_box,
    _parse_window_request,
    _popup_content_box,
    _popup_fills_canvas,
    _popup_window_opened,
    _popup_window_request,
    _prime_visuals,
    _publish_render_artifacts,
    _resolve_popup_crop,
    _wait_for_step_narration,
    run_render,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.slide import SlideOverlay
from guidebot_recorder.tts.base import Segment
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import MuxAudioTrack, compose_popup_video, probe_duration

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg niedostępny"),
]

SCENARIO = textwrap.dedent(
    """\
    config:
      title: Logowanie
      viewport: {width: 640, height: 480}
      tts: {provider: fake, voice: v, lang: pl-PL}
    steps:
      - say: "Witaj, zaraz pokażę logowanie."
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - teach: "kliknij Zaloguj"
    """
)


class MockReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


class LinkReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="link", name="otworz", exact=True),
        )


class TypeReasoner:
    async def resolve(self, instruction, candidates):
        return ReasonerResult(
            action="type",
            target=RoleTarget(role="textbox", name="E-mail", exact=True),
            input_text="koparka@poczta.wp.pl",
        )


class FakeTts:
    adapter_version = 1
    duration = 0.3

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                str(self.duration),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return self.duration


class SlowTts(FakeTts):
    duration = 0.8


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


async def test_compositor_starts_popup_at_verified_visual_frame(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:duration=2:size=640x480:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(main),
        ],
        check=True,
        capture_output=True,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 640, "height": 480},
            record_video_dir=str(tmp_path),
            record_video_size={"width": 640, "height": 480},
        )
        overlay = Overlay(
            CursorConfig(
                width=72,
                height=96,
                color="#ff00ff",
                outline="#ff00ff",
                glow="transparent",
            )
        )
        overlay.pos = (300.0, 200.0)
        chrome = Chrome(ChromeConfig(enabled=True, showUrl=False, barColor="#00ff00"))
        await overlay.install_context(context)
        await chrome.install_context(context)
        page = await context.new_page()
        await page.set_content(
            """<button onclick="
                const child = window.open('about:blank');
                setTimeout(() => {
                    child.document.open();
                    child.document.write('<h1>Final popup</h1>');
                    child.document.close();
                }, 75);
            ">Open</button>"""
        )

        prime_tasks: list[tuple[float, asyncio.Task[float | None]]] = []

        def prime(candidate):
            prime_tasks.append(
                (time.monotonic(), asyncio.create_task(_prime_visuals(candidate, overlay, chrome)))
            )

        context.on("page", prime)
        for run_index in range(5):
            task_count = len(prime_tasks)
            async with page.expect_popup() as popup_info:
                await page.get_by_role("button", name="Open").click()
            popup = await popup_info.value
            assert len(prime_tasks) == task_count + 1
            opened_at, prime_task = prime_tasks[-1]
            visual_ready_at = await prime_task
            assert visual_ready_at is not None

            assert await popup.get_by_role("heading", name="Final popup").count() == 1
            assert await popup.locator("[data-guidebot-cursor]").count() == 1
            assert await popup.locator("[data-guidebot-chrome]").count() == 1
            await popup.wait_for_timeout(300)
            video = popup.video
            assert video is not None
            await popup.close()
            closed_at = time.monotonic()
            webm = Path(await video.path())
            composite = tmp_path / f"composite-{run_index}.mp4"
            timeline_opened_at = 0.2
            compose_popup_video(
                main,
                webm,
                composite,
                opened_at=timeline_opened_at,
                closed_at=timeline_opened_at + (closed_at - opened_at),
                visual_ready_delay=visual_ready_at - opened_at,
            )
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(composite),
                    "-t",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            frame_size = 640 * 480 * 3
            assert len(raw) >= frame_size
            assert len(raw) % frame_size == 0

            def pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
                offset = (y * 640 + x) * 3
                return tuple(frame[offset : offset + 3])

            first_popup_frame = None
            for offset in range(0, len(raw), frame_size):
                frame = raw[offset : offset + frame_size]
                red, green, blue = pixel(frame, 620, 400)
                if red > 180 and green > 180 and blue > 180:
                    first_popup_frame = frame
                    break
            assert first_popup_frame is not None

            red, green, blue = pixel(first_popup_frame, 620, 20)
            assert green > 150 and red < 100 and blue < 100
            cursor_pixels = (
                pixel(first_popup_frame, x, y) for y in range(200, 296) for x in range(300, 372)
            )
            assert any(
                red > 140 and green < 120 and blue > 140 for red, green, blue in cursor_pixels
            )

        await context.close()
        await browser.close()


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


async def test_render_produces_mp4_with_audio(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1


async def test_run_render_registers_overlay_then_slide_then_chrome_init_scripts(
    tmp_path, monkeypatch
):
    """Locks in render.py's context init-script ordering contract.

    cursor.js and slide.js both rely on reading the real ``window.top`` to
    decide whether they are running in the top document or a framed site;
    chrome.js is what shadows ``top`` for frame-bust neutralization. If either
    ran after chrome.js, it would read the shadowed ``top`` and misidentify
    its role. This spies on ``install_context`` (rather than asserting on
    ``window.top`` behavior directly) because modern Chromium already makes
    ``Object.defineProperty(window, "top", ...)`` a no-op for cross-origin
    frames, so a black-box DOM assertion can't distinguish a correct order
    from a swapped one — only the registration order itself can.
    """
    path = tmp_path / "chrome.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Chrome
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              chrome: {enabled: true}
            steps:
              - say: "Witaj."
            """
        ),
        encoding="utf-8",
    )

    order: list[str] = []
    original_overlay_install = Overlay.install_context
    original_slide_install = SlideOverlay.install_context
    original_chrome_install = Chrome.install_context

    async def spy_overlay_install(self, context):
        order.append("overlay")
        return await original_overlay_install(self, context)

    async def spy_slide_install(self, context):
        order.append("slide")
        return await original_slide_install(self, context)

    async def spy_chrome_install(self, context):
        order.append("chrome")
        return await original_chrome_install(self, context)

    monkeypatch.setattr(Overlay, "install_context", spy_overlay_install)
    monkeypatch.setattr(SlideOverlay, "install_context", spy_slide_install)
    monkeypatch.setattr(Chrome, "install_context", spy_chrome_install)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert order == ["overlay", "slide", "chrome"]


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

    async def observe_narration_wait(segments: list[Segment]) -> None:
        narration_waits.append(max(segment.duration for segment in segments))
        await _wait_for_step_narration(segments)

    monkeypatch.setattr(
        "guidebot_recorder.recorder.render._wait_for_step_narration",
        observe_narration_wait,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
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


async def test_render_rejects_sidecar_from_other_scenario_before_tts_or_browser(tmp_path):
    path = tmp_path / "narration.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Narracja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: pl, lang: pl-PL}
            steps:
              - say: "Witaj."
            """
        ),
        encoding="utf-8",
    )
    write_compiled(
        compiled_path(path),
        CompiledScenario(source="english.scenario.yaml", actions=[None]),
    )

    with pytest.raises(RenderError, match="innego scenariusza"):
        await run_render(
            path,
            tmp_path / "out.mp4",
            FakeTts(),
            tmp_path / "cache",
            object(),  # type: ignore[arg-type] -- provenance fails before browser use
        )

    assert not (tmp_path / "cache").exists()


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

    monkeypatch.setattr("guidebot_recorder.recorder.render.os.replace", interrupt_at_master)

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

    def staged_mux(video, tracks, destination, *, preencoded=False, video_duration=None):
        assert video_duration == 1.0
        if failure_point == "mux":
            raise RuntimeError("mux failed")
        destination.write_bytes(b"new master")

    monkeypatch.setattr("guidebot_recorder.recorder.render.build_audio_bed", staged_bed)
    monkeypatch.setattr("guidebot_recorder.recorder.render.mux_audio_tracks", staged_mux)

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


async def test_render_without_cache_raises(tmp_path):
    scenario = textwrap.dedent(
        """\
        config:
          title: t
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "s.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        with pytest.raises(RenderError):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_rejects_old_compiler_version(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        write_compiled(
            cpath,
            compiled.model_copy(update={"compiler_version": COMPILER_VERSION - 1}),
        )

        with pytest.raises(RenderError, match="starszą wersję"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_rejects_teach_text_changed_after_compile(tmp_path):
    scenario = textwrap.dedent(
        """\
        config:
          title: t
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<input aria-label='E-mail'>"
          - teach: "wpisz koparka@poczta.wp.pl w pole E-mail"
        """
    )
    path = tmp_path / "type.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, TypeReasoner())
        await page.context.close()

        path.write_text(
            scenario.replace("koparka@poczta.wp.pl", "nowy@poczta.wp.pl"),
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="compiled jest nieaktualny"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_fails_when_expected_popup_does_not_open(tmp_path):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        action = compiled.actions[2]
        write_compiled(
            cpath,
            compiled.model_copy(
                update={"actions": [None, None, action.model_copy(update={"opens_popup": True})]}
            ),
        )

        with pytest.raises(RenderError, match="oczekiwany popup"):
            await run_render(
                path,
                tmp_path / "out.mp4",
                FakeTts(),
                tmp_path / "cache",
                browser,
                timeout=0.2,
            )
        await browser.close()


async def test_render_fails_when_popup_closes_during_opening(tmp_path):
    popup_html = tmp_path / "popup.html"
    popup_html.write_text("<h1>Popup</h1>", encoding="utf-8")
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "immediate-close.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        popup_html.write_text("<script>window.close()</script>", encoding="utf-8")
        with pytest.raises(RenderError, match="podczas otwierania"):
            await run_render(
                path,
                tmp_path / "out.mp4",
                FakeTts(),
                tmp_path / "cache",
                browser,
            )
        await browser.close()


async def test_render_fails_on_unexpected_popup(tmp_path):
    html = tmp_path / "popup.html"
    html.write_text(
        "<button onclick=\"window.open('about:blank')\">Zaloguj</button>", encoding="utf-8"
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        cpath = compiled_path(path)
        compiled = load_compiled(cpath)
        action = compiled.actions[1]
        assert action.opens_popup is True
        write_compiled(
            cpath,
            compiled.model_copy(
                update={"actions": [None, action.model_copy(update={"opens_popup": False})]}
            ),
        )
        html.write_text(
            "<button onclick=\"setTimeout(() => window.open('about:blank'), 0)\">Zaloguj</button>",
            encoding="utf-8",
        )

        with pytest.raises(RenderError, match="nieoczekiwany popup"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_does_not_attribute_popup_opened_before_actual_click(tmp_path):
    correct = tmp_path / "correct.html"
    correct.write_text("<h1>Correct popup</h1>", encoding="utf-8")
    early = tmp_path / "early.html"
    early.write_text("<h1>Early popup</h1>", encoding="utf-8")
    html = tmp_path / "main.html"
    button = "<button onclick=\"window.open('correct.html')\">Zaloguj</button>"
    html.write_text(button, encoding="utf-8")
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "pre-click-popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        # The timer fires after narration but while Recorder is moving/settling
        # the synthetic cursor, before Locator.click is actually dispatched.
        html.write_text(
            button + "<script>setTimeout(() => window.open('early.html'), 550)</script>",
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="przed akcją click"):
            await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_fails_when_popup_closes_during_narration(tmp_path):
    popup_html = tmp_path / "popup.html"
    popup_html.write_text("<h1>Popup</h1>", encoding="utf-8")
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
          - say: "Popup pozostaje otwarty podczas tej narracji."
        """
    )
    path = tmp_path / "async-close.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        # Keep the compiled target/lifecycle metadata, then simulate runtime drift
        # that closes the popup independently of a scenario action.
        popup_html.write_text(
            "<h1>Popup</h1><script>setTimeout(() => close(), 300)</script>",
            encoding="utf-8",
        )
        with pytest.raises(RenderError, match="asynchronicznie"):
            await run_render(path, tmp_path / "out.mp4", SlowTts(), tmp_path / "cache", browser)
        await browser.close()


async def test_render_wires_viewport_and_typing_animation(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    scenario_with_typing = textwrap.dedent(
        """\
        config:
          title: Logowanie
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
          typing: {animate: true, speed: 55}
        steps:
          - say: "Witaj, zaraz pokażę logowanie."
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "typing.scenario.yaml"
    path.write_text(scenario_with_typing, encoding="utf-8")

    overlay_viewports: list = []
    recorder_kwargs: list = []

    class SpyOverlay(R.Overlay):
        def __init__(self, cursor=None, viewport=None):
            overlay_viewports.append(viewport)
            super().__init__(cursor, viewport)

    class SpyRecorder(R.Recorder):
        def __init__(self, *a, **k):
            recorder_kwargs.append(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(R, "Overlay", SpyOverlay)
    monkeypatch.setattr(R, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert overlay_viewports[0] is not None
    assert overlay_viewports[0].width == 640
    assert any(k.get("type_delay_ms") == 55 for k in recorder_kwargs)


async def test_render_respects_typing_animate_false(tmp_path, monkeypatch):
    # Typing now animates by default (see test_config defaults); this guards the
    # explicit opt-out: `typing.animate: false` must leave the Recorder without a
    # per-character delay so fields fill instantly.
    import guidebot_recorder.recorder.render as R

    scenario = SCENARIO.replace(
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n",
        "  tts: {provider: fake, voice: v, lang: pl-PL}\n  typing: {animate: false}\n",
    )
    path = tmp_path / "no-typing.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    recorder_kwargs: list = []

    class SpyRecorder(R.Recorder):
        def __init__(self, *a, **k):
            recorder_kwargs.append(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(R, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert recorder_kwargs
    assert all(k.get("type_delay_ms") is None for k in recorder_kwargs)


class SoundReasoner:
    """Resolves the 'pole E-mail' textbox target and the 'Zaloguj' button target."""

    async def resolve(self, instruction, candidates):
        if "E-mail" in instruction:
            return ReasonerResult(
                action="type",
                target=RoleTarget(role="textbox", name="E-mail", exact=True),
            )
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


def _sound_scenario(sound_yaml: str) -> str:
    head = textwrap.dedent(
        """\
        config:
          title: Logowanie
          viewport: {width: 640, height: 480}
          tts: {provider: fake, voice: v, lang: pl-PL}
          typing: {animate: true, speed: 40}
        """
    )
    tail = textwrap.dedent(
        """\
        steps:
          - navigate: "data:text/html,<input aria-label='E-mail'><button>Zaloguj</button>"
          - enterText: {into: "pole E-mail", text: "abc"}
          - click: "Zaloguj"
        """
    )
    return head + sound_yaml + tail


async def _compile_sound_scenario(path: Path, sound_yaml: str) -> None:
    path.write_text(_sound_scenario(sound_yaml), encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, SoundReasoner())
        await page.context.close()
        await browser.close()


async def test_render_with_sound_collects_and_mixes_sfx(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "sound-on.scenario.yaml"
    await _compile_sound_scenario(
        path, "  sound: {enabled: true, click: true, keys: true, volume: -12}\n"
    )

    recorded_events: list[list[tuple[str, float]]] = []
    original_build_sfx_bed = R.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        recorded_events.append(list(events))
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R, "build_sfx_bed", spy_build_sfx_bed)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert recorded_events, "build_sfx_bed was never called"
    assert recorded_events[0], "build_sfx_bed was called with an empty events list"
    kinds = {kind for kind, _offset in recorded_events[0]}
    assert "click" in kinds
    assert "key" in kinds
    assert probe_duration(out) > 0


async def test_render_sound_off_builds_no_sfx_bed(tmp_path, monkeypatch):
    # Sound is on by default now; this guards the explicit opt-out: with
    # `sound.enabled: false` no SFX are collected and no bed is ever built.
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "sound-off.scenario.yaml"
    await _compile_sound_scenario(path, "  sound: {enabled: false}\n")

    calls: list = []
    original_build_sfx_bed = R.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        calls.append(events)
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R, "build_sfx_bed", spy_build_sfx_bed)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert calls == []
    assert probe_duration(out) > 0


async def test_render_sound_gates_keys_when_disabled(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "sound-no-keys.scenario.yaml"
    await _compile_sound_scenario(
        path, "  sound: {enabled: true, click: true, keys: false, volume: -12}\n"
    )

    recorded_events: list[list[tuple[str, float]]] = []
    original_build_sfx_bed = R.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        recorded_events.append(list(events))
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R, "build_sfx_bed", spy_build_sfx_bed)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert recorded_events, "build_sfx_bed was never called"
    kinds = [kind for kind, _offset in recorded_events[0]]
    assert "key" not in kinds
    assert "click" in kinds
    assert probe_duration(out) > 0


# --- Slide cards + auto-intro (Task 5.3) -------------------------------------

SLIDE_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Prezentacja
      viewport: {width: 640, height: 480}
      tts: {provider: fake, voice: v, lang: pl-PL}
    steps:
      - slide: {title: "Witaj w GuideBot", hold: 0.05}
      - say: "To jest wprowadzenie."
    """
)


async def test_slide_step_paints_card_and_hides_layers(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide.scenario.yaml"
    path.write_text(SLIDE_SCENARIO, encoding="utf-8")

    slide_events: list[tuple[str, dict]] = []

    class SpySlide(R.SlideOverlay):
        async def show(self, page, card):
            await super().show(page, card)
            slide_events.append(("show", dict(card)))

        async def ensure(self, page, card):
            await super().ensure(page, card)
            dom_count = await page.locator("[data-guidebot-slide]").count()
            cursor_display = await page.evaluate(
                "() => document.querySelector('[data-guidebot-cursor]')?.style.display"
            )
            slide_events.append(
                (
                    "ensure",
                    {"card": dict(card), "dom_count": dom_count, "cursor_display": cursor_display},
                )
            )

    monkeypatch.setattr(R, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    show_events = [payload for kind, payload in slide_events if kind == "show"]
    ensure_events = [payload for kind, payload in slide_events if kind == "ensure"]
    assert show_events, "the slide step never called SlideOverlay.show"
    assert show_events[0] == {
        "title": "Witaj w GuideBot",
        "subtitle": None,
        "notes": None,
    }
    assert ensure_events, "the say step never re-asserted the card via _ensure_card"
    # While the `say` narrates, the card must be mounted and the cursor hidden.
    assert ensure_events[0]["dom_count"] == 1
    assert ensure_events[0]["cursor_display"] == "none"
    assert out.exists()
    assert probe_duration(out) > 0


async def test_teach_or_navigate_after_slide_dismisses_card(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-navigate.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.05}
              - navigate: "data:text/html,<p>Po slajdzie</p>"
            """
        ),
        encoding="utf-8",
    )

    slide_hide_calls = 0
    overlay_show_calls = 0
    dom_state_before_navigate: list[int] = []

    class SpySlide(R.SlideOverlay):
        async def hide(self, page):
            nonlocal slide_hide_calls
            slide_hide_calls += 1
            await super().hide(page)

    class SpyOverlay(R.Overlay):
        async def show(self, page):
            nonlocal overlay_show_calls
            overlay_show_calls += 1
            await super().show(page)

    class SpyRecorder(R.Recorder):
        async def navigate(self, url):
            dom_state_before_navigate.append(
                await self.page.locator("[data-guidebot-slide]").count()
            )
            await super().navigate(url)

    monkeypatch.setattr(R, "SlideOverlay", SpySlide)
    monkeypatch.setattr(R, "Overlay", SpyOverlay)
    monkeypatch.setattr(R, "Recorder", SpyRecorder)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert slide_hide_calls >= 1, "the navigate step never dismissed the card"
    assert overlay_show_calls >= 1, "the navigate step never restored the cursor"
    assert dom_state_before_navigate == [0], "the card was still mounted when navigate ran"
    assert out.exists()


async def test_navigation_destroying_card_mid_say_fails_loud(tmp_path, monkeypatch):
    """GAP 1: the card is destroyed by a navigation DURING the say's narration
    wait, and the say is the LAST step. The pre-narration `_ensure_card` check
    already passed (card was still alive then); only the post-narration re-check
    can catch this. Without it the video narrates over the wrong page and render
    completes silently — so the render MUST raise RenderError instead."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-fail.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja nad znikającą kartą."
            """
        ),
        encoding="utf-8",
    )

    # The card is "destroyed" (token goes falsy, as a fresh navigated document
    # would report) only AFTER the narration wait completes — i.e. the
    # destruction happens DURING the wait, never before it. The pre-narration
    # check therefore sees a live card; only a post-wait check sees the loss.
    destroyed = {"value": False}

    class MidWaitDestroySlide(R.SlideOverlay):
        async def token(self, page):
            if destroyed["value"]:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R, "SlideOverlay", MidWaitDestroySlide)

    original_wait = R._wait_for_step_narration

    async def destroy_during_wait(segments):
        await original_wait(segments)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R, "_wait_for_step_narration", destroy_during_wait)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert destroyed["value"], "the narration-wait wrapper never ran"
    assert not out.exists()


async def test_slide_after_card_destroyed_during_say_fails_loud(tmp_path, monkeypatch):
    """GAP 2 (shape): a `[slide, say, slide]` scenario where the middle say's
    card is destroyed during its narration wait must also raise RenderError —
    the shape must never complete silently.

    The spy models reality: `token` is falsy while destroyed, but a real
    `show()` (the trailing slide's repaint) restores a truthy token. Without the
    post-narration re-check this shape completes SILENTLY, because the trailing
    slide repaints a fresh card over the wrong page, restoring a valid token, so
    that slide's own hold-loop check then passes."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-say-slide.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja nad znikającą kartą."
              - slide: {title: "Krok 2", hold: 0.0}
            """
        ),
        encoding="utf-8",
    )

    destroyed = {"value": False}

    class GhostNavSlide(R.SlideOverlay):
        async def show(self, page, card):
            await super().show(page, card)
            destroyed["value"] = False  # a repaint restores a truthy token

        async def token(self, page):
            if destroyed["value"]:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R, "SlideOverlay", GhostNavSlide)

    original_wait = R._wait_for_step_narration

    async def destroy_during_wait(segments):
        await original_wait(segments)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R, "_wait_for_step_narration", destroy_during_wait)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert not out.exists()


async def test_slide_dismiss_fails_loud_when_card_destroyed_after_say(tmp_path, monkeypatch):
    """GAP 2 (isolates the slide-dismiss token assert): a navigation lands
    AFTER the say has fully completed (so its post-narration re-check already
    passed) but BEFORE the following slide dismisses the card. This reproduces
    the realistic race the slide-dismiss `_assert_card_alive` guards: without
    it, the next slide silently repaints a fresh card (restoring a truthy
    token) over the navigated page, and the render succeeds silently.

    The spy models reality: `token` is falsy while the ghost-navigation is in
    effect, but a real `show()` (the next slide's repaint) restores a truthy
    token — exactly what a fresh document's first `show()` would do."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "slide-say-slide-race.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Prezentacja
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
            steps:
              - slide: {title: "Krok 1", hold: 0.0}
              - say: "Narracja, po której następuje nawigacja."
              - slide: {title: "Krok 2", hold: 0.0}
            """
        ),
        encoding="utf-8",
    )

    slide_ref: dict = {}

    class GhostNavSlide(R.SlideOverlay):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._ghost = False
            slide_ref["slide"] = self

        async def show(self, page, card):
            await super().show(page, card)
            # A real show() in a fresh document bumps the token to truthy; model
            # the repaint restoring a valid token so the bug (silent repaint over
            # the wrong page) is faithfully reproduced.
            self._ghost = False

        async def token(self, page):
            if self._ghost:
                return 0
            return await super().token(page)

    monkeypatch.setattr(R, "SlideOverlay", GhostNavSlide)

    original_render_step = R._render_step

    async def render_step_spy(*args, **kwargs):
        # args[6] is `kind` (see _render_step's signature). A navigation lands
        # the instant the say step finishes — after its post-narration re-check.
        kind = args[6]
        result = await original_render_step(*args, **kwargs)
        if kind == "say":
            slide_ref["slide"]._ghost = True
        return result

    monkeypatch.setattr(R, "_render_step", render_step_spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        with pytest.raises(RenderError, match="karta slajdu zniknęła"):
            await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert not out.exists()


async def test_intro_enabled_replaces_bootstrap(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "intro-on.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Logowanie
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              intro: {enabled: true, subtitle: "Poznaj system", notes: "Zaczynamy"}
            steps:
              - say: "Witaj, zaraz pokażę logowanie."
              - navigate: "data:text/html,<button>Zaloguj</button>"
              - teach: "kliknij Zaloguj"
            """
        ),
        encoding="utf-8",
    )

    show_calls: list[dict] = []

    class SpySlide(R.SlideOverlay):
        async def show(self, page, card):
            show_calls.append(dict(card))
            await super().show(page, card)

    monkeypatch.setattr(R, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert show_calls, "intro.enabled=True never painted a card at bootstrap"
    # The FIRST show() call is the bootstrap intro card (no `slide` step exists
    # in this scenario, so there is no other candidate call).
    assert show_calls[0] == {
        "title": "Logowanie",
        "subtitle": "Poznaj system",
        "notes": "Zaczynamy",
    }
    assert out.exists()
    assert probe_duration(out) > 0


async def test_intro_disabled_bootstrap_unchanged(tmp_path, monkeypatch):
    """The critical back-compat guarantee: `intro.enabled=False` never paints a
    card and never calls SlideOverlay.show — bootstrap is byte-identical to
    pre-Task-5.3 behavior."""

    import guidebot_recorder.recorder.render as R

    path = tmp_path / "intro-off.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")  # intro defaults to disabled

    show_calls: list[dict] = []

    class SpySlide(R.SlideOverlay):
        async def show(self, page, card):
            show_calls.append(dict(card))
            await super().show(page, card)

    monkeypatch.setattr(R, "SlideOverlay", SpySlide)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert show_calls == []
    assert out.exists()
    assert probe_duration(out) > 0


class PopupCursorReasoner:
    """Opens the popup, then closes it from inside the popup."""

    async def resolve(self, instruction, candidates):
        if "Zamknij" in instruction:
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="button", name="Zamknij", exact=True),
            )
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


async def test_render_hands_cursor_over_to_popup_and_back(tmp_path, monkeypatch):
    """The cursor lives in exactly one window at a time.

    While a popup is on screen the main window must not keep painting its own
    synthetic cursor (the compositor shows the main video around/behind the
    popup). On popup close the main window takes the cursor back.
    """

    import guidebot_recorder.recorder.render as R

    popup_html = tmp_path / "popup.html"
    popup_html.write_text(
        '<h1>Popup</h1><button onclick="window.close()">Zamknij</button>',
        encoding="utf-8",
    )
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
          - teach: "kliknij Zamknij w popupie"
          - wait: 0.3
        """
    )
    path = tmp_path / "popup-cursor.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    events: list[tuple[str, str]] = []

    def _role(page) -> str:
        return "popup" if page.url.endswith("popup.html") else "main"

    class SpyOverlay(R.Overlay):
        async def hide(self, page):
            events.append(("hide", _role(page)))
            await super().hide(page)

        async def show(self, page):
            events.append(("show", _role(page)))
            await super().show(page)

    monkeypatch.setattr(R, "Overlay", SpyOverlay)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, PopupCursorReasoner())
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    # The popup's own cursor is never suppressed; only the main window's is.
    assert ("hide", "popup") not in events
    assert ("hide", "main") in events, events
    assert ("show", "main") in events, events
    # Hidden on handover to the popup, revealed again once the popup is gone.
    assert events.index(("hide", "main")) < events.index(("show", "main")), events
    assert events.count(("hide", "main")) == events.count(("show", "main")), events


# --- popup window geometry ---------------------------------------------------
# The context's viewport (and therefore record_video_size) is shared by every
# page, so a popup records onto a main-viewport-sized canvas. The site's own
# window.open features are the deterministic statement of the real window size.


@pytest.mark.parametrize(
    "requested, expected",
    [
        ({"width": 640, "height": 480}, (640, 480)),
        ({"width": 640.4, "height": 480.6}, (640, 480)),
        (None, None),
        ({"width": 0, "height": 480}, None),
        ({"width": -640, "height": 480}, None),
        ({"width": "640", "height": 480}, None),
        ({"width": 640}, None),
    ],
)
def test_parse_window_request(requested, expected):
    assert _parse_window_request(requested) == expected


async def test_popup_window_request_reads_window_open_features(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.set_content(
            "<button onclick=\"window.open('about:blank','p','width=420,height=300')\">go</button>"
        )

        # Nothing opened yet: no geometry, so the compositor must not crop.
        assert await _popup_window_request(page) is None

        async with context.expect_page():
            await page.click("button")

        assert await _popup_window_request(page) == (420, 300)
        await context.close()
        await browser.close()


async def test_popup_window_request_none_without_size_features(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        # A featureless window.open states no size; degrade to today's no-crop path.
        await page.set_content("<button onclick=\"window.open('about:blank','p')\">go</button>")

        async with context.expect_page():
            await page.click("button")

        assert await _popup_window_request(page) is None
        await context.close()
        await browser.close()


async def test_popup_window_request_finds_the_opener_iframe(tmp_path):
    """The click that opens the popup happens inside the shell's site iframe."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.set_content("<iframe id='site' srcdoc=\"<button>go</button>\"></iframe>")
        frame = page.frame_locator("#site")
        await frame.locator("button").evaluate(
            "el => el.onclick = () => window.open('about:blank','p','width=500,height=360')"
        )

        async with context.expect_page():
            await frame.locator("button").click()

        assert await _popup_window_request(page) == (500, 360)
        await context.close()
        await browser.close()


# --- popup window geometry: the lookup must never hang the render ------------
# An ad-heavy page carries iframes whose document never commits an execution
# context. ``Frame.evaluate`` has no timeout, so evaluating on one blocks
# forever — which is exactly how a real render deadlocked right after a popup
# opened. The lookup is therefore concurrent and bounded: one dead frame can no
# longer hide another frame's answer, and can never stall the render.


class _StubFrame:
    """A frame that answers the window.open probe immediately."""

    url = "https://example.test/"

    def __init__(self, requested):
        self._requested = requested

    async def evaluate(self, expression, *args):
        return self._requested


class _HangingFrame:
    """A frame whose execution context never materialises."""

    url = ""

    def __init__(self):
        self.entered = asyncio.Event()

    async def evaluate(self, expression, *args):
        self.entered.set()
        await asyncio.Event().wait()  # never resolves, exactly like the real one


class _StubOpener:
    def __init__(self, frames, main_frame=None):
        self.main_frame = main_frame if main_frame is not None else _StubFrame(None)
        self.frames = [self.main_frame, *frames]


async def test_popup_window_request_answers_despite_a_frame_that_never_answers(monkeypatch):
    """A dead ad iframe must neither hang the lookup nor hide the real answer."""
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    hanging = _HangingFrame()
    # Reverse order puts the hanging frame ahead of the one holding the answer,
    # so a sequential scan would never reach the answer at all.
    opener = _StubOpener([_StubFrame({"width": 420, "height": 300}), hanging])

    started = time.monotonic()
    assert await _popup_window_request(opener) == (420, 300)
    elapsed = time.monotonic() - started

    assert hanging.entered.is_set(), "test did not exercise the hanging frame"
    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"


async def test_popup_window_request_gives_up_when_only_dead_frames_remain(monkeypatch, capsys):
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    started = time.monotonic()
    assert await _popup_window_request(opener) is None
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_popup_window_request_abandons_no_orphan_task(monkeypatch):
    """An abandoned probe must not leave an un-awaited exception behind."""
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    assert await _popup_window_request(opener) is None

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


# --- popup "opened" flag: the lookup must never hang the render either -------
# Mirrors the geometry lookup's hang-resistance tests above using the same
# stub frames — ``_scan_frames_for_window_opened`` is bounded exactly like
# ``_scan_frames_for_window_request``, and ``_popup_window_opened`` now carries
# the same outer hard timeout as ``_popup_window_request``. Unlike the
# geometry lookup, giving up here must still answer ``True`` ("assume a
# popup"), the fail-safe direction that never mounts an address bar on
# uncertainty.


async def test_popup_window_opened_answers_despite_a_frame_that_never_answers(monkeypatch):
    """A dead ad iframe must neither hang the lookup nor hide the real answer."""
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    hanging = _HangingFrame()
    # Reverse order puts the hanging frame ahead of the one holding the answer,
    # so a sequential scan would never reach the answer at all.
    opener = _StubOpener([_StubFrame(True), hanging])

    started = time.monotonic()
    assert await _popup_window_opened(opener) is True
    elapsed = time.monotonic() - started

    assert hanging.entered.is_set(), "test did not exercise the hanging frame"
    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"


async def test_popup_window_opened_gives_up_when_only_dead_frames_remain(monkeypatch, capsys):
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    started = time.monotonic()
    # Unlike the geometry lookup (which gives up to "no crop"), giving up here
    # must still assume a popup, so no address bar gets mounted on uncertainty.
    assert await _popup_window_opened(opener) is True
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_popup_window_opened_abandons_no_orphan_task(monkeypatch):
    """An abandoned probe must not leave an un-awaited exception behind."""
    monkeypatch.setattr(render_module, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    assert await _popup_window_opened(opener) is True

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


async def test_popup_window_request_prefers_the_top_documents_record():
    """The init script republishes on the top document; it wins over stale frames."""
    opener = _StubOpener(
        [_StubFrame({"width": 800, "height": 600})],
        main_frame=_StubFrame({"width": 420, "height": 300}),
    )
    assert await _popup_window_request(opener) == (420, 300)


async def test_popup_window_request_warns_when_nothing_recorded(capsys):
    assert await _popup_window_request(_StubOpener([_StubFrame(None)])) is None
    assert "OSTRZEŻENIE" in capsys.readouterr().err


# --- popup crop level 2: the content bounding box ----------------------------
# A featureless ``window.open(url, name)`` states no size, so the popup's own
# painted content is the next-best witness. ``body``/``documentElement`` are
# useless here (they are the *context* viewport), hence the union over body's
# children; a union that still fills the viewport is refused as degenerate.


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A real dialog: smaller than the viewport in both dimensions.
        ({"x": 8, "y": 12, "width": 300, "height": 200}, (300, 200, 8, 12)),
        # Fractional CSS pixels round outwards so no painted pixel is lost.
        ({"x": 8.6, "y": 12.4, "width": 300.2, "height": 200.9}, (301, 202, 8, 12)),
        # Full-bleed content: >= the degenerate ratio in BOTH dimensions.
        ({"x": 0, "y": 0, "width": 640, "height": 480}, None),
        ({"x": 0, "y": 0, "width": 632, "height": 474}, None),
        # Full-width but short (a banner) is real geometry, not degenerate.
        ({"x": 0, "y": 0, "width": 640, "height": 120}, (640, 120, 0, 0)),
        # Nothing usable.
        (None, None),
        ({"x": 0, "y": 0, "width": 0, "height": 200}, None),
        ({"x": 0, "y": 0, "width": -300, "height": 200}, None),
        ({"x": 0, "y": 0, "width": float("inf"), "height": 200}, None),
        ({"x": 0, "y": 0, "width": "300", "height": 200}, None),
        ({"x": 0, "y": 0, "width": 300}, None),
    ],
)
def test_parse_content_box(raw, expected):
    if isinstance(raw, dict):
        raw = {**raw, "viewportWidth": 640, "viewportHeight": 480}
    assert _parse_content_box(raw) == expected


def test_popup_bbox_degenerate_ratio_is_a_named_threshold():
    assert 0.9 < POPUP_BBOX_DEGENERATE_RATIO < 1.0


async def _content_box_of(html: str) -> tuple[int, int, int, int] | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        page = await context.new_page()
        await page.set_content(html)
        try:
            return await _popup_content_box(page)
        finally:
            await context.close()
            await browser.close()


async def test_popup_content_box_measures_a_small_dialog():
    # ``body`` is a block: it is 640 wide whatever its children do. Measuring the
    # children (and descending through transparent wrappers) is the whole point.
    box = await _content_box_of(
        "<body style='margin:0'>"
        "<div style='width:100%'>"
        "<div style='width:320px;height:240px;background:#fff'>dialog</div>"
        "</div></body>"
    )
    assert box is not None
    width, height, x, y = box
    assert (x, y) == (0, 0)
    assert width == pytest.approx(320, abs=4)
    assert height == pytest.approx(240, abs=4)


async def test_popup_content_box_rejects_full_bleed_content():
    # A 100vw/100vh wrapper genuinely fills the frame: no honest crop exists, so
    # level 2 must decline instead of returning the viewport back.
    box = await _content_box_of(
        "<body style='margin:0'><div style='width:100vw;height:100vh;background:#123'>x</div></body>"
    )
    assert box is None


async def test_popup_content_box_rejects_a_page_that_paints_its_own_background():
    """A popup styling ``body`` is full-bleed: its background IS the window.

    Found on a real recording — a featureless ``window.open`` of a page with
    ``body { background: yellow }`` filled the whole canvas, and measuring only
    body's children cropped the frame down to the black text on top of it.
    """
    box = await _content_box_of(
        "<body style='margin:0;background:rgb(230,210,20)'>"
        "<h1>Logowanie</h1><label>E-mail</label><input>"
        "</body>"
    )
    assert box is None


async def test_popup_content_box_still_measures_an_unstyled_page():
    """The white of a plain document is the canvas, not a painted background."""
    box = await _content_box_of(
        "<body style='margin:0'><div style='width:240px;height:180px;background:#eee'>d</div></body>"
    )
    assert box is not None
    assert box[0] == pytest.approx(240, abs=4)


async def test_popup_content_box_ignores_guidebot_overlay_elements():
    # The cursor overlay is injected into the popup too; it must not inflate the
    # measured window.
    box = await _content_box_of(
        "<body style='margin:0'>"
        "<div style='width:200px;height:150px;background:#fff'>dialog</div>"
        "<div data-guidebot-cursor style='position:fixed;left:600px;top:460px;"
        "width:20px;height:20px;background:red'></div>"
        "</body>"
    )
    assert box is not None
    width, height, _, _ = box
    assert width < 400 and height < 300, box


async def test_popup_content_box_is_bounded_on_a_huge_dom():
    """The walk is capped in the page, so a monstrous DOM cannot stall the shot."""
    started = time.monotonic()
    box = await _content_box_of(
        "<body style='margin:0'><div id='root'></div><script>"
        "const root = document.getElementById('root');"
        "for (let i = 0; i < 20000; i++) {"
        "  const d = document.createElement('div');"
        "  d.textContent = 'x' + i;"
        "  root.appendChild(d);"
        "}</script></body>"
    )
    elapsed = time.monotonic() - started

    # Whatever it answers (a box, or None because it ran out of budget), it must
    # answer fast: this cost would otherwise land in the recorded video.
    assert elapsed < 2.0, f"content-box walk was not bounded: {elapsed:.2f}s"
    assert box is None or box[0] > 0


# --- popup crop level 2: the measurement must never hang the render ----------
# Same failure mode the window.open lookup had: ``Page.evaluate`` has no timeout,
# so a document that never commits an execution context never answers.


class _HangingPage:
    """A page whose evaluate never resolves."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def evaluate(self, expression, *args):
        self.entered.set()
        await asyncio.Event().wait()


class _SlowPage:
    """A page that answers, but only long after the caller stopped waiting."""

    async def evaluate(self, expression, *args):
        await asyncio.sleep(30)
        return {"x": 0, "y": 0, "width": 10, "height": 10}


def _popup_with_probe(page):
    """A ``_PopupSession`` carrying only what the measurement path touches."""
    session = object.__new__(render_module._PopupSession)
    session.content_box = None
    session.content_box_probe = render_module._start_popup_content_box(page)
    return session


async def test_settle_popup_content_box_gives_up_on_a_page_that_never_answers(monkeypatch, capsys):
    monkeypatch.setattr(render_module, "_POPUP_CONTENT_BOX_TIMEOUT", 0.3)
    page = _HangingPage()
    popup = _popup_with_probe(page)

    started = time.monotonic()
    await render_module._settle_popup_content_box(popup)
    elapsed = time.monotonic() - started

    assert page.entered.is_set(), "test did not exercise the hanging page"
    assert popup.content_box is None
    assert elapsed < 5.0, f"measurement was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_settle_popup_content_box_abandons_no_orphan_task(monkeypatch):
    monkeypatch.setattr(render_module, "_POPUP_CONTENT_BOX_TIMEOUT", 0.3)
    popup = _popup_with_probe(_SlowPage())

    await render_module._settle_popup_content_box(popup)

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


async def test_settle_popup_content_box_keeps_a_timely_answer():
    class _PromptPage:
        async def evaluate(self, expression, *args):
            return {
                "x": 4,
                "y": 6,
                "width": 320,
                "height": 240,
                "viewportWidth": 640,
                "viewportHeight": 480,
            }

    popup = _popup_with_probe(_PromptPage())
    await render_module._settle_popup_content_box(popup)

    assert popup.content_box == (320, 240, 4, 6)
    assert popup.content_box_probe is None


async def test_starting_the_content_box_probe_does_not_block():
    """Its cost must be off camera: starting it returns at once."""

    class _SlowishPage:
        async def evaluate(self, expression, *args):
            await asyncio.sleep(0.5)
            return None

    started = time.monotonic()
    probe = render_module._start_popup_content_box(_SlowishPage())
    elapsed = time.monotonic() - started

    assert elapsed < 0.05, f"starting the probe blocked for {elapsed:.3f}s"
    assert not probe.done()
    await asyncio.gather(probe, return_exceptions=True)


async def test_popup_content_box_survives_a_closed_page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        page = await context.new_page()
        await page.close()
        assert await _popup_content_box(page) is None
        await context.close()
        await browser.close()


# --- popup crop: the three-level fallback chain ------------------------------


def test_resolve_popup_crop_prefers_the_window_open_features(tmp_path, capsys):
    crop, level = _resolve_popup_crop(
        window_size=(420, 300),
        content_box=(320, 240, 4, 6),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (420, 300, 0, 0)
    assert level == "window.open"
    assert "window.open" in capsys.readouterr().out


def test_resolve_popup_crop_falls_back_to_the_content_box(tmp_path, capsys):
    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=(320, 240, 4, 6),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (320, 240, 4, 6)
    assert level == "bbox"
    assert "bbox" in capsys.readouterr().out


def test_resolve_popup_crop_falls_back_to_cropdetect(tmp_path, monkeypatch, capsys):
    seen: list[Path] = []

    def fake_detect(path):
        seen.append(Path(path))
        return (300, 220, 2, 2)

    monkeypatch.setattr(render_module, "detect_content_crop", fake_detect)

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=None,
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (300, 220, 2, 2)
    assert level == "cropdetect"
    assert seen == [tmp_path / "popup.webm"]
    assert "cropdetect" in capsys.readouterr().out


def test_resolve_popup_crop_without_any_geometry_emits_no_crop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_module, "detect_content_crop", lambda path: None)

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=None,
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    # Back-compat: today's full-canvas filtergraph, and the log says so.
    assert crop is None
    assert level == "none"
    assert "cropdetect" in capsys.readouterr().out


def test_resolve_popup_crop_does_not_run_cropdetect_when_a_level_answered(tmp_path, monkeypatch):
    def forbidden(path):  # pragma: no cover - failure path
        raise AssertionError("cropdetect must stay the last resort")

    monkeypatch.setattr(render_module, "detect_content_crop", forbidden)

    assert (
        _resolve_popup_crop(
            window_size=None,
            content_box=(320, 240, 0, 0),
            popup_video=tmp_path / "popup.webm",
            verbose=False,
        )[1]
        == "bbox"
    )


def test_resolve_popup_crop_scales_the_window_open_rect_into_recording_pixels(
    tmp_path, monkeypatch, capsys
):
    # A headed browser on a HiDPI screen composites the popup at the screen's
    # backing scale, so Playwright fits a 500x670 window into the 1376x800 canvas
    # at 1.19x instead of 1:1 — the CSS rect would cut 96px off the right and
    # 130px off the bottom.
    monkeypatch.setattr(render_module, "detect_content_crop", lambda path: (596, 800, 0, 0))

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (596, 800, 0, 0)
    assert level == "window.open"
    assert "1.19" in capsys.readouterr().out


def test_resolve_popup_crop_scales_the_content_box_into_recording_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(render_module, "detect_content_crop", lambda path: (1000, 800, 0, 0))

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=(320, 240, 4, 6),
        viewport=(500, 400),
        canvas=(1000, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    # Scale 2x, origin floored and far edges ceiled so rounding never shaves ink.
    assert crop == (640, 480, 8, 12)
    assert level == "bbox"


def test_resolve_popup_crop_keeps_css_pixels_when_the_recording_is_unscaled(tmp_path, monkeypatch):
    # Headless: the popup records 1:1, so ``detect_content_crop`` finds the whole
    # window already trimmed and declines. The CSS rect is then already correct.
    monkeypatch.setattr(render_module, "detect_content_crop", lambda path: None)

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    assert crop == (500, 670, 0, 0)
    assert level == "window.open"


@pytest.mark.parametrize(
    "measured",
    [
        (900, 300, 0, 0),  # aspect contradicts the 500x670 window
        (596, 800, 10, 4),  # Playwright anchors the window at the origin
        (120, 160, 0, 0),  # 0.24x — no backing scale produces it
    ],
)
def test_resolve_popup_crop_refuses_a_measurement_that_contradicts_the_viewport(
    tmp_path, monkeypatch, measured
):
    monkeypatch.setattr(render_module, "detect_content_crop", lambda path: measured)

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    # A measurement that cannot be a scaled popup window is not evidence about
    # the scale; fall back to 1:1 rather than crop to a guess.
    assert crop == (500, 670, 0, 0)
    assert level == "window.open"


def test_resolve_popup_crop_skips_the_scale_probe_without_a_viewport(tmp_path, monkeypatch):
    def forbidden(path):  # pragma: no cover - failure path
        raise AssertionError("nothing to correct against, so nothing to measure")

    monkeypatch.setattr(render_module, "detect_content_crop", forbidden)

    crop, _level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=None,
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    assert crop == (500, 670, 0, 0)


class _CursorPage:
    """A page that records nothing but its own liveness and viewport."""

    def __init__(self, viewport=None, closed=False):
        self.viewport_size = viewport
        self._closed = closed

    def is_closed(self):
        return self._closed


class _RecordingOverlay:
    """Captures the cursor calls `_hand_cursor_to_popup` makes, in order."""

    def __init__(self, pos=(0.0, 0.0)):
        self.pos = pos
        self.calls = []

    async def hide(self, page):
        self.calls.append(("hide", page))

    async def show(self, page):
        self.calls.append(("show", page))

    async def move_to(self, page, x, y, ms=None):
        self.calls.append(("move_to", page, x, y, ms))
        self.pos = (x, y)


async def test_hand_cursor_to_popup_centres_and_reveals_the_popup_cursor():
    """The popup's cursor must be visible on arrival, not on first action.

    Without this the popup's own cursor instance inherits ``Overlay.pos`` — the
    opener's coordinates, usually the control that opened the popup — and paints
    nothing until something moves it.
    """

    main = _CursorPage()
    popup_page = _CursorPage(viewport={"width": 500, "height": 670})
    popup = render_module._PopupSession(page=popup_page, video=None, opened_at=0.0)
    overlay = _RecordingOverlay(pos=(1300.0, 40.0))

    await render_module._hand_cursor_to_popup(main, popup, overlay)

    assert overlay.calls == [
        ("hide", main),
        ("move_to", popup_page, 250.0, 335.0, 0),
        ("show", popup_page),
    ]
    # ms=0: a glide would animate in from the *other* window's coordinates.
    assert overlay.calls[1][4] == 0


async def test_hand_cursor_to_popup_remembers_where_the_main_cursor_stood():
    """Centring in the popup overwrites the shared position; it must be restorable."""

    popup = render_module._PopupSession(
        page=_CursorPage(viewport={"width": 500, "height": 670}),
        video=None,
        opened_at=0.0,
    )

    await render_module._hand_cursor_to_popup(
        _CursorPage(), popup, _RecordingOverlay(pos=(1300.0, 40.0))
    )

    assert popup.main_cursor_pos == (1300.0, 40.0)


async def test_hand_cursor_to_popup_skips_centring_without_a_viewport():
    """An unknown viewport costs the centring, never the render."""

    popup = render_module._PopupSession(page=_CursorPage(None), video=None, opened_at=0.0)
    overlay = _RecordingOverlay()
    main = _CursorPage()

    await render_module._hand_cursor_to_popup(main, popup, overlay)

    assert overlay.calls == [("hide", main)]


# --- closeWindow ---------------------------------------------------------------


def _write_close_window_scenario(
    tmp_path: Path, *, popup_config: bool = True, chrome: bool = False
) -> Path:
    # A data: page cannot open a new window onto another data: URL (Chromium
    # blocks it outright), so the popup destination needs a real file:// URL —
    # same convention as test_compile.py's closeWindow test. The page paints its
    # own full-bleed background so it genuinely fills the tab: that makes the
    # popup's content bounding box decline outright (see `paintsPage` in
    # `_POPUP_CONTENT_BOX_SCRIPT`) and leaves cropdetect nothing to trim either —
    # matching a real `_blank` tab, where every crop level declines because
    # there is no smaller window to name.
    second = tmp_path / "second.html"
    second.write_text(
        "<!doctype html><html><head><style>body{margin:0;background:#2a6ebb}</style>"
        "</head><body><p>druga</p></body></html>",
        encoding="utf-8",
    )
    main = tmp_path / "main.html"
    main.write_text(
        f"<a href='{second.resolve().as_uri()}' target='_blank'>otworz</a>",
        encoding="utf-8",
    )
    # popup_config=False drops the explicit `popup:` block so the default
    # `float` transition applies — needed to prove a canvas-filling tab gets
    # overridden to `slide` rather than testing a scenario that already asks
    # for `slide`.
    popup_line = "  popup: {transition: slide, slideMs: 40}" if popup_config else ""
    # chrome=True turns the whole browser-chrome feature on, which is what makes
    # the `_blank` tab's address bar observable at all: without it the render
    # builds no `Chrome` controller and there is no bar to mount anywhere.
    chrome_line = "  chrome: {enabled: true}" if chrome else ""
    scenario = textwrap.dedent(
        f"""\
        config:
          title: Karta
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        {popup_line}
        {chrome_line}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy do glownego okna."
        """
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    return path


async def test_close_window_returns_to_main_and_restores_the_cursor(tmp_path):
    path = _write_close_window_scenario(tmp_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0


async def test_close_window_hands_the_cursor_back_to_its_pre_popup_position(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    restored: list[tuple[float, float] | None] = []
    original = R._prepare_main_after_popup_close

    async def spy(page, overlay, chrome, settle_ms, restore_cursor_to=None):
        restored.append(restore_cursor_to)
        await original(page, overlay, chrome, settle_ms, restore_cursor_to=restore_cursor_to)

    monkeypatch.setattr(R, "_prepare_main_after_popup_close", spy)

    path = _write_close_window_scenario(tmp_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert restored, "closeWindow never routed through the popup-close handler"
    assert restored[0] is not None, (
        "the cursor was handed back without its pre-popup position -- the main "
        "window's cursor will be parked at the popup's centre"
    )


# --- _popup_fills_canvas --------------------------------------------------------


def test_popup_fills_canvas_for_a_declined_crop():
    # Every level declining is the `_blank` tab case: no witness could name a
    # smaller window, so the recording *is* the window.
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas(None, Viewport(width=1376, height=800)) is True


def test_popup_fills_canvas_for_a_full_cover_rect():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 0, 0), Viewport(width=1376, height=800)) is True


def test_popup_does_not_fill_canvas_for_a_real_window():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((520, 640, 0, 0), Viewport(width=1376, height=800)) is False


def test_popup_does_not_fill_canvas_when_offset():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 12, 12), Viewport(width=1376, height=800)) is False


async def test_a_full_canvas_popup_is_presented_full_frame_not_inset(tmp_path, monkeypatch):
    # `float` is the default; a `_blank` tab must still render full-frame.
    import guidebot_recorder.recorder.render as R

    seen: list[str | None] = []
    original = R.compose_popup_video

    def spy(*args, **kwargs):
        seen.append(kwargs.get("transition"))
        return original(*args, **kwargs)

    monkeypatch.setattr(R, "compose_popup_video", spy)

    path = _write_close_window_scenario(tmp_path, popup_config=False)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen == ["slide"], f"expected the full-canvas tab to force slide, got {seen}"


async def test_window_open_call_is_recorded_even_without_size_features():
    # A featureless `window.open` must be distinguishable from no call at all:
    # only the latter is a `target=_blank` tab.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_init_script(script=render_module._POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.goto("data:text/html,<p>opener</p>")

        assert await render_module._popup_window_opened(page) is False

        await page.evaluate("window.open('about:blank', 'named')")
        assert await render_module._popup_window_opened(page) is True
        assert await render_module._popup_window_request(page) is None

        await browser.close()


def _start_static_http_server(body: bytes) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Serve ``body`` at ``/`` on an ephemeral 127.0.0.1 port; return (server, thread, origin)."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence the test server
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


async def test_popup_window_opened_is_true_from_a_cross_origin_opener_iframe():
    """The click that opens the popup happens inside a genuinely cross-origin
    site iframe — unlike ``test_popup_window_request_finds_the_opener_iframe``,
    which uses ``srcdoc`` and is therefore same-origin with the opener's top
    document. Here the iframe's ``realTop[OPENED] = true`` mirror write throws
    a real cross-origin ``SecurityError`` (swallowed by the init script's own
    ``catch``), so only a per-frame scan — not a top-frame-only read — can see
    that ``window.open`` was called at all.
    """

    inner_body = (
        b"<!doctype html><body>"
        b"<button id='go' onclick=\"window.open('about:blank','p','width=500,height=400')\">"
        b"go</button></body>"
    )
    inner_server, inner_thread, inner_origin = _start_static_http_server(inner_body)
    outer_body = (
        f"<!doctype html><body><iframe id='site' src='{inner_origin}/'></iframe></body>"
    ).encode()
    outer_server, outer_thread, outer_origin = _start_static_http_server(outer_body)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 640, "height": 480})
            await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
            page = await context.new_page()
            await page.goto(outer_origin)
            frame = page.frame_locator("#site")

            # Nothing opened yet, and the two documents are on distinct origins.
            assert await render_module._popup_window_opened(page) is False

            async with context.expect_page():
                await frame.locator("#go").click()

            assert await render_module._popup_window_opened(page) is True
            # The sibling geometry lookup already handled this correctly; must
            # keep doing so unchanged.
            assert await _popup_window_request(page) == (500, 400)

            await context.close()
            await browser.close()
    finally:
        inner_server.shutdown()
        inner_thread.join()
        outer_server.shutdown()
        outer_thread.join()


# --- the address bar on a `target="_blank"` tab --------------------------------
# `bare_popups` (float/slide) is a context-wide init-script flag, so it strips the
# legacy in-DOM bar from every top-level non-shell document. A genuine `_blank`
# tab is a real browser tab, though, and reads as a rendering fault without an
# address bar — so it, and only it, mounts the bar per page.


async def test_blank_tab_gets_an_address_bar_while_other_popups_stay_bare(tmp_path, monkeypatch):
    # `_blank` is the case where `window.open` was never called at all -- that,
    # not the crop verdict, is what is knowable while the window is still being
    # recorded (the crop chain only answers once the recording is over, and the
    # bar is painted DOM that would corrupt crop levels 2 and 3).
    from guidebot_recorder.chrome import Chrome as ChromeController

    mounted: list[bool] = []
    original = ChromeController.install_bar

    async def spy(self, page):
        await original(self, page)
        mounted.append(await page.query_selector("[data-guidebot-chrome]") is not None)

    monkeypatch.setattr(ChromeController, "install_bar", spy)

    path = _write_close_window_scenario(tmp_path, chrome=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert mounted == [True], (
        "a `_blank` tab must mount the legacy address bar exactly once, and it "
        f"must actually be in the popup's DOM afterwards (got {mounted!r})"
    )


async def test_sized_window_open_popup_never_mounts_the_address_bar(tmp_path, monkeypatch):
    # The counterpart: a popup the site really did open with `window.open` stays
    # bare, exactly as today -- the compositor frames it in post-process. This is
    # what pins the per-window seam as per-window rather than a global flip.
    from guidebot_recorder.chrome import Chrome as ChromeController

    calls: list[str] = []

    async def spy(self, page):
        calls.append(page.url)

    monkeypatch.setattr(ChromeController, "install_bar", spy)

    second = tmp_path / "second.html"
    second.write_text(
        "<!doctype html><html><head><style>body{margin:0;background:#2a6ebb}</style>"
        "</head><body><p>druga</p></body></html>",
        encoding="utf-8",
    )
    main = tmp_path / "main.html"
    main.write_text(
        "<a href='#' onclick=\"window.open("
        f"'{second.resolve().as_uri()}','p','width=420,height=300');return false\">otworz</a>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: Popup
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
          popup: {{transition: slide, slideMs: 40}}
          chrome: {{enabled: true}}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy do glownego okna."
        """
    )
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner())
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert calls == [], f"a sized window.open popup must stay bare (install_bar called for {calls})"
