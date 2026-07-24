"""Shared scaffolding for the popup compile+render E2E tests.

Explicitly imported (no ``conftest.py`` by repo decision). Holds the pixel- and
audio-inspection helpers, the fake reasoner/TTS doubles, the shared floating
scenario template, and — critically — ``PYTESTMARK``, the marker block every
popup E2E module must re-export verbatim (``pytestmark`` does NOT propagate
through an import), so CI selection stays identical across the split files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.reasoner import ReasonerResult

# Re-export in each module as ``pytestmark = PYTESTMARK``. A dropped marker
# silently changes CI selection, so this is the single source of truth.
PYTESTMARK = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe niedostępne",
    ),
]

FIXTURE = Path(__file__).parent / "fixtures" / "popup-main.html"

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
