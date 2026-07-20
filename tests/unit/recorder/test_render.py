import asyncio
import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from pydantic import ValidationError

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import (
    MIN_HOLD_FRAME_SETTLE,
    ChromeConfig,
    CursorConfig,
    TtsConfig,
)
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import (
    _POPUP_REQUEST_SCRIPT,
    RenderError,
    _apply_timeline_edits,
    _assemble_audio_tracks,
    _build_timeline,
    _mux_tracks_for_timeline,
    _pace_narration,
    _parse_window_request,
    _popup_window_request,
    _prime_visuals,
    _publish_render_artifacts,
    run_render,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.slide import SlideOverlay
from guidebot_recorder.tts.base import Segment
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import MuxAudioTrack, compose_popup_video, probe_duration
from guidebot_recorder.video.timeline import TimeEdit, Timeline, probe_frame_count

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

    async def observe_narration_wait(segments: list[Segment], **kwargs) -> None:
        narration_waits.append(max(segment.duration for segment in segments))
        await _pace_narration(segments, **kwargs)

    monkeypatch.setattr(
        "guidebot_recorder.recorder.render._pace_narration",
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

    original_wait = R._pace_narration

    async def destroy_during_wait(segments, **kwargs):
        await original_wait(segments, **kwargs)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R, "_pace_narration", destroy_during_wait)

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

    original_wait = R._pace_narration

    async def destroy_during_wait(segments, **kwargs):
        await original_wait(segments, **kwargs)
        destroyed["value"] = True  # a navigation replaced the document mid-say

    monkeypatch.setattr(R, "_pace_narration", destroy_during_wait)

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


class _Seg:
    def __init__(self, duration: float) -> None:
        self.duration = duration


async def test_pace_narration_sleeps_in_full_when_disabled() -> None:
    edits: list[TimeEdit] = []
    started = time.monotonic()
    await _pace_narration([_Seg(0.3)], anchor=started, hold_frame=False, settle=0.1, edits=edits)
    assert time.monotonic() - started >= 0.3
    assert edits == []


async def test_pace_narration_records_a_freeze_for_the_remainder() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration([_Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits)
    elapsed = time.monotonic() - anchor
    # Only the settle is paid in real time.
    assert elapsed < 1.0
    assert len(edits) == 1
    assert edits[0].kind == "freeze"
    # 2.0s narration - 0.1s settle = 1.9s -> 48 frames (rounded to the grid)
    assert edits[0].frames == 48


async def test_pace_narration_uses_the_longest_language() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration(
        [_Seg(0.5), _Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits
    )
    assert edits[0].frames == 48


async def test_pace_narration_emits_no_freeze_when_narration_is_shorter_than_settle() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration([_Seg(0.2)], anchor=anchor, hold_frame=True, settle=1.0, edits=edits)
    assert time.monotonic() - anchor >= 0.2
    assert edits == []


async def test_pace_narration_ignores_empty_segments() -> None:
    edits: list[TimeEdit] = []
    await _pace_narration([], anchor=time.monotonic(), hold_frame=True, settle=1.0, edits=edits)
    assert edits == []


async def test_run_render_hold_frame_overrides_reach_the_pacing_decision(tmp_path, monkeypatch):
    # The CLI passes its flags as keyword overrides because `run_render` loads
    # the scenario itself — mutating a caller-side Config would be a no-op. This
    # asserts the override really lands on the pacing call, not just on `cfg`.
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0.2
            steps:
              - say: "Krok pierwszy."
            """
        ),
        encoding="utf-8",
    )

    pacing_kwargs: list[dict] = []
    original = R._pace_narration

    async def spy(segments, **kwargs):
        pacing_kwargs.append(kwargs)
        await original(segments, **kwargs)

    monkeypatch.setattr(R, "_pace_narration", spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        await run_render(
            path,
            tmp_path / "out.mp4",
            FakeTts(),
            tmp_path / "cache",
            browser,
            hold_frame=False,
            hold_frame_settle=0.75,
        )
        await browser.close()

    assert pacing_kwargs, "narration pacing never ran"
    assert pacing_kwargs[0]["hold_frame"] is False, "the --no-hold-frame override was discarded"
    assert pacing_kwargs[0]["settle"] == 0.75, "the --hold-frame-settle override was discarded"


async def test_run_render_uses_the_scenario_value_when_no_override_is_given(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold-default.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: false
              holdFrameSettle: 0.25
            steps:
              - say: "Krok pierwszy."
            """
        ),
        encoding="utf-8",
    )

    pacing_kwargs: list[dict] = []
    original = R._pace_narration

    async def spy(segments, **kwargs):
        pacing_kwargs.append(kwargs)
        await original(segments, **kwargs)

    monkeypatch.setattr(R, "_pace_narration", spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert pacing_kwargs[0]["hold_frame"] is False
    assert pacing_kwargs[0]["settle"] == 0.25


class LongTts(FakeTts):
    duration = 3.0


async def test_hold_frame_film_matches_the_model_exactly(tmp_path, monkeypatch):
    """The finished film is exactly as long as the time model says it is.

    This is the deterministic form of "hold-frame preserves the pacing": the
    earlier version rendered the scenario twice and compared the two durations
    within a tolerance, but the no-hold baseline is pure wall clock and drifted
    run to run — the tolerance was absorbing that jitter rather than proving
    anything. Frame counts on both sides are integers, so they can be compared
    for equality.
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0.4
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
            """
        ),
        encoding="utf-8",
    )

    seen: list[Timeline] = []
    original = R._apply_timeline_edits

    def spy(source, timeline, dest):
        seen.append(timeline)
        return original(source, timeline, dest)

    monkeypatch.setattr(R, "_apply_timeline_edits", spy)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()
        await run_render(path, out, LongTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen, "two 3.0s narrations under a 0.4s settle emitted no freezes"
    timeline = seen[0]
    # The point of the feature, stated deterministically: the recording the
    # browser produced is shorter than the film that ships.
    assert timeline.source_frames < timeline.virtual_frames
    # ...and the file on disk is exactly what the model promised.
    assert probe_frame_count(out) == timeline.virtual_frames

    # The audio beds are built from the model's duration, so they must line up
    # with the file the model produced — coverage the unedited path cannot give.
    beds = list((tmp_path / ".guidebot_video" / "out").glob("bed-*.wav"))
    assert beds, "no narration bed was published"
    for bed in beds:
        assert probe_duration(bed) == pytest.approx(probe_duration(out), abs=0.05)


def test_zero_settle_is_rejected_at_scenario_load(tmp_path) -> None:
    """`holdFrameSettle: 0` is sub-frame and must never reach the recorder.

    It used to be a legal config value: the pacing loop stamped several steps
    onto the same 25fps frame, and the strict `Timeline` rejected that — but
    only after the whole recording had already completed. Worse, once that
    crash was fixed, settle=0 silently rendered a film with narration offsets
    collapsed onto shared timestamps (see `Config.hold_frame_settle`). The fix
    is to reject it at config validation, before any recording happens.
    """
    path = tmp_path / "zero-settle.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zero
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
              - say: "Trzeci."
              - say: "Czwarty."
              - say: "Piąty."
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_scenario(path)


async def test_smallest_legal_settle_still_renders(tmp_path, monkeypatch):
    """`Config`'s smallest legal `holdFrameSettle` still renders end-to-end.

    This replaces the old settle=0 render test now that 0 is rejected at
    config validation (see `test_zero_settle_is_rejected_at_scenario_load`).
    It proves the render pipeline still holds a still frame and produces a
    file matching the model at the *smallest value `Config` actually accepts*
    — the merge/clamp logic in `_build_timeline` itself stays covered
    independently of this test, by the pure `_build_timeline` unit tests
    below (they build a `Timeline` directly and need no legal `Config` at
    all).
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "min-settle.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: MinSettle
              viewport: {{width: 640, height: 480}}
              tts: {{provider: fake, voice: v, lang: pl-PL}}
              holdFrameForNarration: true
              holdFrameSettle: {MIN_HOLD_FRAME_SETTLE}
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
            """
        ),
        encoding="utf-8",
    )

    seen: list[Timeline] = []
    original = R._apply_timeline_edits

    def spy(source, timeline, dest):
        seen.append(timeline)
        return original(source, timeline, dest)

    monkeypatch.setattr(R, "_apply_timeline_edits", spy)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner())
        await page.context.close()
        await run_render(path, out, LongTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen, "no freezes were recorded at the minimum legal settle"
    timeline = seen[0]
    # Two 3.0s narrations, almost none of it paid in real time: the finished
    # film is well past what the browser actually recorded, and the file on
    # disk is exactly what the model promised — the same shape of assertion
    # `test_hold_frame_film_matches_the_model_exactly` makes, at the opposite
    # (minimum legal) end of the settle range.
    assert timeline.source_frames < timeline.virtual_frames
    assert probe_frame_count(out) == timeline.virtual_frames


def test_build_timeline_merges_two_freezes_on_the_same_frame() -> None:
    # Two edits landing on the same frame index both want the picture held
    # there, so the film holds it for the total of both. `_build_timeline`
    # takes raw `TimeEdit`s directly, bypassing `Config` and its
    # `hold_frame_settle` floor, so this stays exercised regardless of what
    # settle values `Config` accepts.
    timeline = _build_timeline(
        [
            TimeEdit(at=40, kind="freeze", frames=25),
            TimeEdit(at=40, kind="freeze", frames=50),
        ],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=40, kind="freeze", frames=75),)
    assert timeline.virtual_frames == 100 + 75


def test_build_timeline_merges_three_freezes_on_the_same_frame() -> None:
    timeline = _build_timeline(
        [TimeEdit(at=7, kind="freeze", frames=n) for n in (10, 20, 30)],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=7, kind="freeze", frames=60),)
    assert timeline.virtual_frames == 100 + 60


def test_build_timeline_clamps_a_freeze_past_the_end() -> None:
    timeline = _build_timeline(
        [TimeEdit(at=140, kind="freeze", frames=30)],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=99, kind="freeze", frames=30),)
    assert timeline.virtual_frames == 100 + 30


def test_build_timeline_clamps_a_freeze_exactly_at_source_frames_and_merges() -> None:
    # The postroll is only 0.1s, so a freeze stamped at the very end rounds onto
    # (or past) the last frame. Clamping happens before merging, so a clamped
    # edit coalesces with one already sitting on the last frame.
    timeline = _build_timeline(
        [
            TimeEdit(at=99, kind="freeze", frames=12),
            TimeEdit(at=100, kind="freeze", frames=13),
        ],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=99, kind="freeze", frames=25),)
    assert timeline.virtual_frames == 100 + 25


def test_build_timeline_keeps_distinct_frames_apart() -> None:
    timeline = _build_timeline(
        [
            TimeEdit(at=10, kind="freeze", frames=5),
            TimeEdit(at=20, kind="freeze", frames=7),
        ],
        source_frames=100,
    )
    assert timeline.edits == (
        TimeEdit(at=10, kind="freeze", frames=5),
        TimeEdit(at=20, kind="freeze", frames=7),
    )
    assert timeline.virtual_frames == 100 + 12


def test_apply_timeline_edits_rejects_a_file_that_disagrees_with_the_model(
    tmp_path, monkeypatch
) -> None:
    # Nothing downstream re-probes the video: `mux_audio_tracks` is handed the
    # model's own duration, so its tolerance compares the model against itself.
    # This is the only place the model meets reality.
    import guidebot_recorder.recorder.render as R

    timeline = Timeline.build([TimeEdit(at=10, kind="freeze", frames=25)], source_frames=100)
    monkeypatch.setattr(R, "apply_time_edits", lambda src, tl, out: None)
    monkeypatch.setattr(R, "probe_frame_count", lambda path: 123)

    with pytest.raises(RenderError) as excinfo:
        _apply_timeline_edits(tmp_path / "src.mp4", timeline, tmp_path / "out.mp4")

    message = str(excinfo.value)
    assert "123" in message
    assert str(timeline.virtual_frames) in message
