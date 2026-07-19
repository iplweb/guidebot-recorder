"""E2E: active-page compile and main → popup → main video assembly."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.video.mux import probe_duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe niedostępne",
    ),
]

FIXTURE = Path(__file__).parent / "fixtures" / "popup-main.html"

SCENARIO_TEMPLATE = """\
config:
  title: Popup logowania
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v-pl, lang: pl-PL, trackLanguage: pol, title: Polski}}
  audioTracks:
    - {{provider: fake, voice: v-en, lang: en-US, trackLanguage: eng, title: English}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
    translations: {{en-US: "Open the login popup"}}
  - teach: "przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl"
    translations: {{en-US: "switch to the popup and type koparka@poczta.wp.pl in email"}}
  - wait: 0.4
  - click: "Zamknij popup logowania"
  - click: "Zakończ na stronie głównej"
"""

OPEN_POPUP_SCENARIO_TEMPLATE = """\
config:
  title: Popup pozostający otwarty
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
  - teach: "Wpisz w pole email tekst koparka@poczta.wp.pl"
  - wait: 0.4
"""

FLOATING_SCENARIO_TEMPLATE = """\
config:
  title: Popup logowania (floating)
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: true, scale: 0.72, backdropDim: 0.5}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
  - teach: "przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl"
  - wait: 0.4
  - click: "Zamknij popup logowania"
  - click: "Zakończ na stronie głównej"
"""


class PopupReasoner:
    def __init__(self) -> None:
        self.calls = 0
        self.popup_candidates_seen = False

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "Otwórz" in instruction:
            return ReasonerResult(
                "click", RoleTarget(role="button", name="Otwórz logowanie", exact=True)
            )
        if "koparka@" in instruction:
            self.popup_candidates_seen = any(
                candidate.role == "textbox" and candidate.name == "E-mail"
                for candidate in candidates
            )
            return ReasonerResult(
                "type",
                RoleTarget(role="textbox", name="E-mail", exact=True),
                input_text="koparka@poczta.wp.pl",
            )
        if "Zamknij" in instruction:
            return ReasonerResult(
                "click", RoleTarget(role="button", name="Zamknij logowanie", exact=True)
            )
        return ReasonerResult(
            "click",
            RoleTarget(role="button", name="Zakończ na stronie głównej", exact=True),
        )


class NoCallsReasoner:
    async def resolve(self, instruction, candidates):  # pragma: no cover - failure path
        raise AssertionError(f"cache should resolve {instruction!r} without Reasoner")


class FakeTts:
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        duration = 0.4
        frequency = {"pl-PL": 440, "en-US": 880}.get(tts.lang, 660)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={duration}:sample_rate=48000",
                "-t",
                str(duration),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return duration


def _stream_types(path: Path) -> list[str]:
    output = subprocess.run(
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
    return [line.strip() for line in output.splitlines() if line.strip()]


def _audio_streams(path: Path) -> list[dict]:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream_tags=language,handler_name:stream_disposition=default",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output)["streams"]


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


def _rgb_at(path: Path, seconds: float) -> tuple[int, int, int]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{seconds:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(raw) == 3
    return raw[0], raw[1], raw[2]


def _rgb_at_pixel(
    path: Path,
    seconds: float,
    x: int = 620,
    y: int = 20,
) -> tuple[int, int, int]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{seconds:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"crop=2:2:{x}:{y},scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(raw) == 3
    return raw[0], raw[1], raw[2]


def _has_audio_signal(path: Path, start: float, seconds: float = 0.1) -> bool:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{seconds:.6f}",
            "-i",
            str(path),
            "-vn",
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
    samples = [
        int.from_bytes(raw[index : index + 2], "little", signed=True)
        for index in range(0, len(raw) - 1, 2)
    ]
    return bool(samples) and max(map(abs, samples)) > 100


def _is_main_blue(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    return blue > 120 and blue > red + 60 and blue > green + 60


def _is_popup_yellow(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    return red > 120 and green > 120 and blue < 100


def _is_chrome_gray(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    return abs(red - green) < 20 and abs(green - blue) < 20 and red > 180


async def test_popup_compile_reuse_and_render_composite(tmp_path: Path) -> None:
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()), encoding="utf-8")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        reasoner = PopupReasoner()
        await run_compile(path, compile_page, reasoner)
        await compile_page.context.close()

        compiled = load_compiled(compiled_path(path))
        opening = compiled.actions[2]
        typing = compiled.actions[3]
        assert opening is not None and opening.opens_popup is True
        assert typing is not None and typing.action == "type"
        assert typing.input_text == "koparka@poczta.wp.pl"
        assert reasoner.popup_candidates_seen is True
        assert reasoner.calls == 4

        # Reuse still executes the opening click, follows the popup and validates
        # popup-only identities without asking the Reasoner again.
        reuse_page = await browser.new_page()
        await run_compile(path, reuse_page, NoCallsReasoner())
        await reuse_page.context.close()

        out = tmp_path / "popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 2
    audio_streams = _audio_streams(out)
    assert [stream["tags"]["language"] for stream in audio_streams] == ["pol", "eng"]
    assert [stream["tags"]["handler_name"] for stream in audio_streams] == [
        "Polski",
        "English",
    ]
    assert [stream["disposition"]["default"] for stream in audio_streams] == [1, 0]
    assert _decode_audio_stream(out, 0) != _decode_audio_stream(out, 1)

    samples = [_rgb_at(out, duration * fraction / 20) for fraction in range(1, 20)]
    main_indices = [index for index, rgb in enumerate(samples) if _is_main_blue(rgb)]
    popup_indices = [index for index, rgb in enumerate(samples) if _is_popup_yellow(rgb)]
    assert main_indices and popup_indices
    assert min(main_indices) < min(popup_indices)
    assert max(main_indices) > max(popup_indices)
    popup_sample_time = duration * (min(popup_indices) + 1) / 20
    before_popup_windows = [index / 10 for index in range(1, int(popup_sample_time * 10))]
    popup_windows = [index / 10 for index in range(int(popup_sample_time * 10), int(duration * 10))]
    assert any(_has_audio_signal(out, start) for start in before_popup_windows)
    assert any(_has_audio_signal(out, start) for start in popup_windows)
    for sample_index in (min(main_indices), min(popup_indices), max(main_indices)):
        sample_time = duration * (sample_index + 1) / 20
        assert _is_chrome_gray(_rgb_at_pixel(out, sample_time))


async def test_floating_popup_renders_as_inset_over_visible_main(tmp_path: Path) -> None:
    """Floating mode: the bare popup renders end-to-end (prime stabilizes without
    a chrome bar) and composites as a centred inset over the still-visible main
    page — never the full-frame hard cut."""

    path = tmp_path / "floating-popup.scenario.yaml"
    path.write_text(
        FLOATING_SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner())
        await compile_page.context.close()

        out = tmp_path / "floating-popup.mp4"
        # A raise here (e.g. "nie udało się zainicjować warstw wizualnych") would
        # mean the bare-popup prime seam regressed; reaching the asserts proves it
        # stabilized without a [data-guidebot-chrome] bar on the popup page.
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 1

    # Sample centre vs left edge across the timeline. The defining property of the
    # floating composite is that the dimmed MAIN page (blue) shows at the border
    # while the popup (not blue) fills the centre inset. A hard cut would paint the
    # popup edge-to-edge, so the border is never the main page during the popup
    # interval. We key on the main-blue backdrop rather than the popup's exact
    # centre colour, which depends on how the popup content renders (fonts/layout
    # differ across platforms) and is not a reliable single-pixel signal.
    inset_frame_found = False
    for fraction in range(1, 40):
        seconds = duration * fraction / 40
        centre = _rgb_at_pixel(out, seconds, x=319, y=239)
        edge = _rgb_at_pixel(out, seconds, x=4, y=239)
        if _is_main_blue(edge) and not _is_main_blue(centre):
            inset_frame_found = True
            break
    assert inset_frame_found, "expected a floating popup inset over a visible main backdrop"


async def test_popup_left_open_stays_visible_through_video_end(tmp_path: Path) -> None:
    path = tmp_path / "open-popup.scenario.yaml"
    path.write_text(
        OPEN_POPUP_SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner())
        await compile_page.context.close()

        out = tmp_path / "open-popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 1

    samples = [_rgb_at(out, duration * fraction / 20) for fraction in range(1, 20)]
    main_indices = [index for index, rgb in enumerate(samples) if _is_main_blue(rgb)]
    popup_indices = [index for index, rgb in enumerate(samples) if _is_popup_yellow(rgb)]
    assert main_indices and popup_indices
    assert min(main_indices) < min(popup_indices)
    assert _is_popup_yellow(samples[-1])
