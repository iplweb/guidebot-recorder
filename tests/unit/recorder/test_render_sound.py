"""``recorder.render`` SFX bed: collecting, mixing and gating step sounds.

Split out of the original ``test_render.py`` (audio pipeline, SFX half).
"""

import textwrap
from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.video.mux.probe import probe_duration

from ._render_helpers import FFMPEG, FakeTts

pytestmark = FFMPEG


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
        await run_compile(path, page, SoundReasoner(), selects=None)
        await page.context.close()
        await browser.close()


async def test_render_with_sound_collects_and_mixes_sfx(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "sound-on.scenario.yaml"
    await _compile_sound_scenario(
        path, "  sound: {enabled: true, click: true, keys: true, volume: -12}\n"
    )

    recorded_events: list[list[tuple[str, float]]] = []
    original_build_sfx_bed = R.audio.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        recorded_events.append(list(events))
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R.audio, "build_sfx_bed", spy_build_sfx_bed)

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
    original_build_sfx_bed = R.audio.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        calls.append(events)
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R.audio, "build_sfx_bed", spy_build_sfx_bed)

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
    original_build_sfx_bed = R.audio.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        recorded_events.append(list(events))
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R.audio, "build_sfx_bed", spy_build_sfx_bed)

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
