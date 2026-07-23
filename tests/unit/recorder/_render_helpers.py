"""Shared scaffolding for the ``test_render_*`` suite (no ``conftest`` by repo policy).

Exports the FFMPEG marker block, the login ``SCENARIO`` and the fake reasoners and
TTS used across the split files. Import what a file needs explicitly; there are no
fixtures here (the render tests open their own browser inline).
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.reasoner import ReasonerResult

FFMPEG = [
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


class LongTts(FakeTts):
    duration = 3.0


class TwoSecondTts(FakeTts):
    duration = 2.0
