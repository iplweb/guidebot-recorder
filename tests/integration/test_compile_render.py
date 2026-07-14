"""E2E: pełny compile → render na statycznym fixture logowania.

Reasoner zamockowany (deterministyczny), TTS fałszywy (ciche mp3) — bez sieci
i bez Codexa. Weryfikuje, że pętla „scenariusz → film" działa end-to-end.
"""

from __future__ import annotations

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
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg niedostępny"),
]

FIXTURE = Path(__file__).parent / "fixtures" / "app.html"

SCENARIO_TEMPLATE = """\
config:
  title: Logowanie
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Pokażę, jak się zalogować."
  - navigate: "{url}"
  - teach: "kliknij Zaloguj"
  - enterText: {{into: "pole email", text: "user@x.pl"}}
    say: "Wpisuję adres e-mail."
"""


class MockReasoner:
    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "Zaloguj" in instruction:
            return ReasonerResult("click", RoleTarget(role="button", name="Zaloguj"))
        return ReasonerResult("type", RoleTarget(role="textbox", name="E-mail"))


class FakeTts:
    adapter_version = 1

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
                "0.3",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return 0.3


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


async def test_end_to_end_compile_then_render(tmp_path):
    url = FIXTURE.resolve().as_uri()
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO_TEMPLATE.format(url=url), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # --- compile ---
        page = await browser.new_page()
        reasoner = MockReasoner()
        await run_compile(path, page, reasoner)
        await page.context.close()

        compiled = load_compiled(compiled_path(path))
        teach_ca = compiled.actions[2]
        enter_ca = compiled.actions[3]
        assert teach_ca.action == "click"
        assert teach_ca.identity.tag == "button"
        assert enter_ca.action == "type"
        assert enter_ca.identity.tag == "input"
        assert enter_ca.input_text is None
        assert "user@x.pl" not in compiled_path(path).read_text(encoding="utf-8")
        assert reasoner.calls == 2

        # --- re-compile: reuse, zero wywołań reasonera ---
        page2 = await browser.new_page()
        reasoner2 = MockReasoner()
        await run_compile(path, page2, reasoner2)
        await page2.context.close()
        assert reasoner2.calls == 0

        # --- render ---
        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1
