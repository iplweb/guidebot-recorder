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

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import ChromeConfig, CursorConfig, TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import (
    RenderError,
    _assemble_audio_tracks,
    _mux_tracks_for_timeline,
    _prime_visuals,
    _publish_render_artifacts,
    _wait_for_step_narration,
    run_render,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
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


def test_audio_timeline_rejects_even_subframe_narration_overrun(tmp_path):
    tts = TtsConfig(
        provider="fake",
        voice="pl",
        lang="pl-PL",
        trackLanguage="pol",
    )
    segment = Segment(text="koniec", path=tmp_path / "unused.mp3", duration=0.08)

    with pytest.raises(RenderError, match="wykracza poza nagranie"):
        _mux_tracks_for_timeline(
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
def test_assemble_failure_preserves_previous_master_and_complete_bed_set(
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

    def staged_mux(video, tracks, destination, *, preencoded=False):
        if failure_point == "mux":
            raise RuntimeError("mux failed")
        destination.write_bytes(b"new master")

    monkeypatch.setattr("guidebot_recorder.recorder.render.build_audio_bed", staged_bed)
    monkeypatch.setattr("guidebot_recorder.recorder.render.mux_audio_tracks", staged_mux)

    with pytest.raises(RuntimeError, match="failed"):
        _assemble_audio_tracks(
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
