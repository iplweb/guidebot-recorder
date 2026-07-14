"""Faza `render` — deterministyczne odtworzenie + montaż filmu (§8/§9).

Faza 0: pre-synteza całej narracji do cache (brak wywołań TTS „na żywo").
Render: 0×LLM, świeża przeglądarka, jedno przejście; narracja steruje tempem.
Montaż: wideo Playwrighta + audio bed (ffmpeg), sync przybliżony (decyzja K2).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Browser

from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.tts.base import Segment, TtsCache, TtsProvider
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import mux, probe_duration


class RenderError(RuntimeError):
    """Krok wymaga (re-)compile: brak `cachedAction` lub niezgodna tożsamość."""


def _narration(step: Step) -> str | None:
    if step.say:
        return step.say
    if step.teach:
        return step.teach
    return None


def _resolve_url(scenario: Scenario, url: str) -> str:
    base = scenario.config.base_url
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


async def run_render(
    path: Path | str,
    out_mp4: Path | str,
    tts_provider: TtsProvider,
    cache_dir: Path | str,
    browser: Browser,
) -> None:
    path = Path(path)
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    loaded = load_scenario(path)
    scenario = loaded.scenario
    cfg = scenario.config

    # --- Faza 0: pre-synteza całej narracji (fail-loud przed nagrywaniem) ---
    cache = TtsCache(cache_dir)
    segments: dict[int, Segment] = {}
    for index, step in enumerate(scenario.steps):
        text = _narration(step)
        if text:
            segments[index] = await cache.get_or_synth(text, cfg.tts, tts_provider)

    # --- Render z nagrywaniem wideo ---
    work = out_mp4.parent / ".guidebot_video"
    work.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        record_video_dir=str(work),
        record_video_size={"width": cfg.viewport.width, "height": cfg.viewport.height},
    )
    page = await context.new_page()
    video = page.video
    overlay = Overlay()
    await overlay.install(page)
    recorder = Recorder(page, overlay)

    placed: list[Placed] = []
    anchor = time.monotonic()

    for index, step in enumerate(scenario.steps):
        kind = step.command_kind()

        seg = segments.get(index)
        if seg is not None:
            placed.append(Placed(segment=seg, offset=time.monotonic() - anchor))
            await asyncio.sleep(seg.duration)  # narracja steruje tempem

        if kind == "say":
            continue
        if kind == "navigate":
            await recorder.navigate(_resolve_url(scenario, step.navigate))
            await overlay.ensure(page)
            continue
        if kind == "wait" and not step.requires_target():
            await recorder.wait_seconds(float(step.wait))
            continue

        cached = step.cached_action
        if cached is None:
            raise RenderError(f"krok {index}: brak cachedAction — uruchom `compile`")
        if cached.action != "waitFor" and not await reuse_is_valid(page, cached):
            raise RenderError(f"krok {index}: niezgodna tożsamość — uruchom `compile`")

        await overlay.ensure(page)
        if cached.action == "click":
            await recorder.click(cached.target)
        elif cached.action == "hover":
            await recorder.hover(cached.target)
        elif cached.action == "type":
            await recorder.enter_text(cached.target, step.enter_text.text)
        elif cached.action == "waitFor":
            timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
            await recorder.wait_for(cached.target, cached.state or "visible", timeout)
        await recorder.apply_readiness(cached.expect)

    await page.close()
    await context.close()

    webm = Path(await video.path())
    total = probe_duration(webm)
    bed = work / "bed.wav"
    build_audio_bed(placed, total, bed)
    mux(webm, bed, out_mp4)
