"""E2E: two localized browser runs with independent actions, audio, and video."""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.render_set import run_compile_set, run_render_set
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.render_set import load_render_set
from guidebot_recorder.video.mux import probe_duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe unavailable",
    ),
]

FIXTURE = Path(__file__).parent / "fixtures" / "localized-app.html"

SCENARIOS = {
    "pl-PL": """\
config:
  title: Logowanie po polsku
  viewport: {width: 800, height: 600}
  locale: pl-PL
  tts:
    {provider: fake, voice: v-pl, lang: pl-PL, trackLanguage: pol, title: Polski}
steps:
  - navigate: "{url}"
  - teach: "Kliknij przycisk Zaloguj"
  - say: "Logowanie zostało pokazane."
""",
    "en-US": """\
config:
  title: Login in English
  viewport: {width: 800, height: 600}
  locale: en-US
  tts:
    {provider: fake, voice: v-en, lang: en-US, trackLanguage: eng, title: English}
steps:
  - navigate: "{url}"
  - teach: "Click the Sign in button"
  - say: "The login flow is complete."
""",
}

MANIFEST = """\
kind: localized-render-set
version: 1
variants:
  pl-PL:
    scenario: login.pl.scenario.yaml
    output: login.pl.mp4
  en-US:
    scenario: login.en.scenario.yaml
    output: login.en.mp4
"""


class LocalizedReasoner:
    """Resolve only when instruction and live localized candidates agree."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, instruction, candidates):  # noqa: ANN001
        if "Zaloguj" in instruction:
            language, name = "pl-PL", "Zaloguj"
        elif "Sign in" in instruction:
            language, name = "en-US", "Sign in"
        else:  # pragma: no cover - a failure should show the unexpected instruction
            raise AssertionError(f"unexpected localized instruction: {instruction!r}")

        assert any(
            candidate.role == "button" and candidate.name == name for candidate in candidates
        ), f"localized candidate {name!r} absent for {language}"
        self.calls.append((language, instruction))
        return ReasonerResult("click", RoleTarget(role="button", name=name, exact=True))


class LocalizedToneTts:
    adapter_version = 1
    frequencies = {"pl-PL": 440, "en-US": 880}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        frequency = self.frequencies[tts.lang]
        duration = 0.3
        self.calls.append((text, tts.lang, tts.voice))
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


def _probe_streams(path: Path) -> list[dict]:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels:"
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


def _decode_audio(path: Path) -> tuple[int, ...]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
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
    return struct.unpack(f"<{len(raw) // 2}h", raw)


def _goertzel_power(samples: tuple[int, ...], frequency: int, sample_rate: int = 8000) -> float:
    """Measure one known fixture tone without adding a numeric dependency."""

    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency / sample_rate)
    previous = 0.0
    previous_previous = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_previous
        previous_previous, previous = previous, current
    return (
        previous_previous * previous_previous
        + previous * previous
        - coefficient * previous * previous_previous
    )


def _sample_background(path: Path) -> tuple[int, int, int]:
    at = probe_duration(path) * 0.9
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{at:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "crop=2:2:790:590,scale=1:1",
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


def _assert_single_localized_audio(path: Path, language: str, title: str) -> None:
    streams = _probe_streams(path)
    assert [stream["codec_type"] for stream in streams] == ["video", "audio"]
    assert streams[0]["codec_name"] == "h264"
    audio = streams[1]
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2
    assert audio["tags"]["language"] == language
    assert audio["tags"]["handler_name"] == title
    assert audio["disposition"]["default"] == 1


async def test_localized_render_set_has_independent_actions_audio_and_picture(
    tmp_path: Path,
) -> None:
    fixture_url = FIXTURE.resolve().as_uri()
    polish = tmp_path / "login.pl.scenario.yaml"
    english = tmp_path / "login.en.scenario.yaml"
    polish.write_text(SCENARIOS["pl-PL"].replace("{url}", fixture_url), encoding="utf-8")
    english.write_text(SCENARIOS["en-US"].replace("{url}", fixture_url), encoding="utf-8")
    manifest = tmp_path / "localized.render-set.yaml"
    manifest.write_text(MANIFEST, encoding="utf-8")

    plan = load_render_set(manifest)
    out_dir = tmp_path / "out"
    provider = LocalizedToneTts()

    # The set API intentionally receives Browser (not Page): it must create a fresh
    # locale-specific context for every compile and render variant.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        first_reasoner = LocalizedReasoner()
        first_result = await run_compile_set(plan, browser, first_reasoner)
        assert first_result.compiled == ("pl-PL", "en-US")
        assert first_result.reused == ()
        assert first_reasoner.calls == [
            ("pl-PL", "Kliknij przycisk Zaloguj"),
            ("en-US", "Click the Sign in button"),
        ]

        polish_compiled = load_compiled(compiled_path(polish))
        english_compiled = load_compiled(compiled_path(english))
        polish_action = polish_compiled.actions[1]
        english_action = english_compiled.actions[1]
        assert polish_action is not None and english_action is not None
        assert polish_action.target == RoleTarget(role="button", name="Zaloguj", exact=True)
        assert english_action.target == RoleTarget(role="button", name="Sign in", exact=True)
        assert polish_action.fingerprint.compiled_from == "Kliknij przycisk Zaloguj"
        assert english_action.fingerprint.compiled_from == "Click the Sign in button"
        assert polish_action.fingerprint.config_hash != english_action.fingerprint.config_hash

        reuse_reasoner = LocalizedReasoner()
        reuse_result = await run_compile_set(plan, browser, reuse_reasoner)
        assert reuse_result.compiled == ()
        assert reuse_result.reused == ("pl-PL", "en-US")
        assert reuse_reasoner.calls == []

        outputs = await run_render_set(
            plan,
            out_dir,
            provider,
            tmp_path / "cache",
            browser,
        )
        await browser.close()

    expected_outputs = [out_dir / "login.pl.mp4", out_dir / "login.en.mp4"]
    assert outputs == expected_outputs
    assert all(path.exists() and probe_duration(path) > 0 for path in outputs)
    _assert_single_localized_audio(outputs[0], "pol", "Polski")
    _assert_single_localized_audio(outputs[1], "eng", "English")

    calls_by_language = Counter(lang for _text, lang, _voice in provider.calls)
    assert calls_by_language == {"pl-PL": 2, "en-US": 2}
    assert {text for text, lang, _voice in provider.calls if lang == "pl-PL"} == {
        "Kliknij przycisk Zaloguj",
        "Logowanie zostało pokazane.",
    }
    assert {text for text, lang, _voice in provider.calls if lang == "en-US"} == {
        "Click the Sign in button",
        "The login flow is complete.",
    }

    polish_samples = _decode_audio(outputs[0])
    english_samples = _decode_audio(outputs[1])
    assert _goertzel_power(polish_samples, 440) > _goertzel_power(polish_samples, 880) * 10
    assert _goertzel_power(english_samples, 880) > _goertzel_power(english_samples, 440) * 10

    polish_rgb = _sample_background(outputs[0])
    english_rgb = _sample_background(outputs[1])
    assert polish_rgb[2] > polish_rgb[0] + 80 and polish_rgb[2] > polish_rgb[1] + 80
    assert english_rgb[1] > english_rgb[0] + 80 and english_rgb[1] > english_rgb[2] + 60
