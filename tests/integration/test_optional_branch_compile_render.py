"""E2E: optional `when` branches through compile → render on a local fixture.

The fixture's cookie banner is toggled by a flag written into the page itself,
so the *same* URL — and therefore the same `.compiled.yaml` — can be rendered
once with the banner and once without. The banner is injected by `setTimeout`,
which is the canonical shape of the problem: a single snapshot right after
`navigate` would report a spurious absence and silently drop the branch.

Reasoner mocked (deterministic, decides purely from the candidate list), TTS
faked — no network, no Codex.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.video.mux.probe import probe_duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe niedostępne",
    ),
]

FIXTURE = Path(__file__).parent / "fixtures" / "optional-banner.html"
_FLAG_DEFAULTS = 'data-banner="0" data-banner-delay="0"'

GATE = "baner zgody na ciasteczka"
ACCEPT = "kliknij przycisk akceptujący ciasteczka"
ACCOUNT = "kliknij Moje konto"

BANNER_HEADING = ("heading", "Zgoda na ciasteczka")
BANNER_BUTTON = ("button", "Akceptuję ciasteczka")

# flat step indices: 0 navigate, 1 gate, 2 teach (child), 3 say (child), 4 teach
GATE_INDEX = 1
CHILD_INDEX = 2
#: nagłówek bannera bramki — numer 1-based z mianownikiem, plus `plik:linia`
#: (`when:` stoi w 7. linii SCENARIO_TEMPLATE) i człon o rodzaju kroku
GATE_BANNER = f"⚠ krok {GATE_INDEX + 1}/5 — "
GATE_LOCATION = "shop.scenario.yaml:7 (bramka `when:`)"

SCENARIO_TEMPLATE = """\
config:
  title: Sklep
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - navigate: "{url}"
  - when: "{gate}"
    timeout: {timeout}
    steps:
      - teach: "{accept}"
      - say: "Akceptujemy ciasteczka."
  - teach: "{account}"
"""


def _write_fixture(tmp_path: Path, *, banner: bool, delay_ms: int = 0) -> str:
    """Write the fixture to a stable path so the scenario URL never changes."""

    source = FIXTURE.read_text(encoding="utf-8")
    flags = f'data-banner="{int(banner)}" data-banner-delay="{delay_ms}"'
    patched = source.replace(_FLAG_DEFAULTS, flags)
    assert patched != source or flags == _FLAG_DEFAULTS, "nie podmieniono flag fixture'u"
    target = tmp_path / "shop.html"
    target.write_text(patched, encoding="utf-8")
    return target.resolve().as_uri()


def _write_scenario(tmp_path: Path, url: str, *, timeout: float) -> Path:
    path = tmp_path / "shop.scenario.yaml"
    path.write_text(
        SCENARIO_TEMPLATE.format(
            url=url, gate=GATE, accept=ACCEPT, account=ACCOUNT, timeout=timeout
        ),
        encoding="utf-8",
    )
    return path


class BannerReasoner:
    """Answers only from what is actually on the page, so absence is real."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, instruction, candidates):
        self.calls.append(instruction)
        visible = {(candidate.role, candidate.name) for candidate in candidates}
        if instruction == GATE:
            if BANNER_HEADING not in visible:
                return ReasonerError("no_handle", "banera nie ma na stronie")
            return ReasonerResult(
                "waitFor", RoleTarget(role="heading", name=BANNER_HEADING[1], exact=True)
            )
        if instruction == ACCEPT:
            if BANNER_BUTTON not in visible:
                return ReasonerError("no_handle", "przycisku zgody nie ma na stronie")
            return ReasonerResult(
                "click", RoleTarget(role="button", name=BANNER_BUTTON[1], exact=True)
            )
        return ReasonerResult("click", RoleTarget(role="button", name="Moje konto", exact=True))

    def count(self, instruction: str) -> int:
        return self.calls.count(instruction)


class NoCallsReasoner:
    async def resolve(self, instruction, candidates):  # pragma: no cover - failure path
        raise AssertionError(f"cache powinien rozwiązać {instruction!r} bez reasonera")


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


def _assert_playable(out: Path) -> None:
    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1


async def test_same_compiled_renders_with_and_without_banner(tmp_path, capsys) -> None:
    """One `.compiled.yaml`, two renders: the branch runs, then is skipped.

    Nothing about the sidecar changes between the runs — only the page does — so
    a frozen gate must tolerate its own `wait_for` timeout and drop the whole
    branch, while the step after the branch still renders.
    """

    url = _write_fixture(tmp_path, banner=True)
    path = _write_scenario(tmp_path, url, timeout=3)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        reasoner = BannerReasoner()
        await run_compile(path, compile_page, reasoner, selects=None)
        await compile_page.context.close()

        # The banner was up: gate and child are fully frozen, nothing pending.
        compiled = load_compiled(compiled_path(path))
        gate = compiled.actions[GATE_INDEX]
        child = compiled.actions[CHILD_INDEX]
        assert isinstance(gate, CachedAction) and gate.action == "waitFor"
        assert isinstance(child, CachedAction) and child.action == "click"
        assert reasoner.calls == [GATE, ACCEPT, ACCOUNT]
        frozen_before = compiled_path(path).read_text(encoding="utf-8")

        # --- render #1: banner present, branch taken ---
        with_banner = tmp_path / "with-banner.mp4"
        await run_render(
            path,
            with_banner,
            FakeTts(),
            tmp_path / "cache",
            browser,
            reasoner=NoCallsReasoner(),
        )

        # --- render #2: same sidecar, banner gone, branch skipped ---
        _write_fixture(tmp_path, banner=False)
        without_banner = tmp_path / "without-banner.mp4"
        await run_render(
            path,
            without_banner,
            FakeTts(),
            tmp_path / "cache",
            browser,
            reasoner=NoCallsReasoner(),
        )
        await browser.close()

    _assert_playable(with_banner)
    _assert_playable(without_banner)

    captured = capsys.readouterr()
    assert GATE_BANNER in captured.out
    assert GATE_LOCATION in captured.out
    assert "bramka pominięty" in captured.out
    # The gate timed out; that is not an error, and the sidecar is untouched.
    assert compiled_path(path).read_text(encoding="utf-8") == frozen_before


async def test_absent_banner_compiles_to_pending_and_renders_skipped(tmp_path, capsys) -> None:
    """Compile without the banner: pending entries, a warning, and a normal return.

    The render that follows has no Reasoner at all — the documented "codex not
    installed" case — and must skip the branch rather than fail.
    """

    url = _write_fixture(tmp_path, banner=False)
    path = _write_scenario(tmp_path, url, timeout=8)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        reasoner = BannerReasoner()
        await run_compile(path, compile_page, reasoner, selects=None)
        await compile_page.context.close()

        compiled = load_compiled(compiled_path(path))
        assert isinstance(compiled.actions[GATE_INDEX], PendingAction)
        assert isinstance(compiled.actions[CHILD_INDEX], PendingAction)
        assert compiled.actions[3] is None  # `say` needs no target
        assert isinstance(compiled.actions[4], CachedAction)
        # The gate was absent, so the child was never even offered to the Reasoner.
        assert reasoner.calls == [GATE, ACCOUNT]

        out = tmp_path / "no-banner.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser, reasoner=None)
        await browser.close()

    _assert_playable(out)

    captured = capsys.readouterr()
    # Compile warns once, for the gate; the children follow it silently into
    # pending because the branch as a whole never happened.
    assert GATE_BANNER in captured.out
    assert GATE_LOCATION in captured.out
    assert "element bramkujący" in captured.out
    assert "bramka pominięty" in captured.out
    # Fragment YAML-a cytowany dosłownie ze źródła bramki.
    assert f'  - when: "{GATE}"' in captured.out
    # Nothing was resolved, so the sidecar stays pending.
    still = load_compiled(compiled_path(path))
    assert isinstance(still.actions[GATE_INDEX], PendingAction)
    assert isinstance(still.actions[CHILD_INDEX], PendingAction)


async def test_delayed_banner_is_resolved_in_place_and_then_cached(tmp_path, capsys) -> None:
    """The self-healing promise, end to end.

    Compile never saw the banner. The render does — but only 2.5 s after
    `navigate`, so the pending gate is only resolvable if the resolution *polls*
    instead of taking one snapshot. Once resolved it is written back as a
    `CachedAction`, and the next render of that branch needs no Reasoner at all.
    """

    url = _write_fixture(tmp_path, banner=False)
    path = _write_scenario(tmp_path, url, timeout=8)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, BannerReasoner(), selects=None)
        await compile_page.context.close()

        compiled = load_compiled(compiled_path(path))
        assert isinstance(compiled.actions[GATE_INDEX], PendingAction)
        assert isinstance(compiled.actions[CHILD_INDEX], PendingAction)

        # --- render #1: banner shows up late; the branch heals itself ---
        _write_fixture(tmp_path, banner=True, delay_ms=2500)
        healing = BannerReasoner()
        first = tmp_path / "healing.mp4"
        await run_render(path, first, FakeTts(), tmp_path / "cache", browser, reasoner=healing)

        # A one-shot resolve would have seen no banner and skipped the branch.
        # More than one gate call is the polling loop, and the child call only
        # happens because the poll eventually succeeded.
        assert healing.count(GATE) > 1, healing.calls
        assert healing.count(ACCEPT) == 1
        assert healing.count(ACCOUNT) == 0  # already frozen by compile

        resolved = load_compiled(compiled_path(path))
        gate = resolved.actions[GATE_INDEX]
        child = resolved.actions[CHILD_INDEX]
        assert isinstance(gate, CachedAction)
        assert gate.action == "waitFor"
        assert gate.identity is not None and gate.identity.tag == "h2"
        assert isinstance(child, CachedAction)
        assert child.action == "click"
        assert child.identity is not None and child.identity.tag == "button"

        # --- render #2: the branch is now deterministic ---
        second = tmp_path / "cached.mp4"
        await run_render(
            path,
            second,
            FakeTts(),
            tmp_path / "cache",
            browser,
            reasoner=NoCallsReasoner(),
        )
        await browser.close()

    _assert_playable(first)
    _assert_playable(second)

    captured = capsys.readouterr()
    assert "bramka pominięty" not in captured.out
