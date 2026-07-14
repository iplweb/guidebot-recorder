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

from playwright.async_api import Browser, Page
from tqdm import tqdm

from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
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
    *,
    timeout: float = 30.0,
    pause_on_error: bool = False,
    verbose: bool = False,
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
    presynth = tqdm(scenario.steps, desc="tts", unit="krok", disable=not verbose)
    for index, step in enumerate(presynth):
        text = _narration(step)
        if text:
            segments[index] = await cache.get_or_synth(text, cfg.tts, tts_provider)
    presynth.close()

    # --- Render z nagrywaniem wideo (viewport z config — patrz compile) ---
    work = out_mp4.parent / ".guidebot_video"
    work.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        record_video_dir=str(work),
        record_video_size={"width": cfg.viewport.width, "height": cfg.viewport.height},
    )
    page = await context.new_page()
    page.set_default_timeout(timeout * 1000)
    video = page.video
    overlay = Overlay()
    await overlay.install(page)
    recorder = Recorder(page, overlay)

    placed: list[Placed] = []
    anchor = time.monotonic()

    bar = tqdm(total=len(scenario.steps), desc="render", unit="krok", disable=not verbose)
    try:
        for index, step in enumerate(scenario.steps):
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(scenario.steps)}] {kind}")
            try:
                await _render_step(
                    page, recorder, overlay, scenario, step, kind, index, segments, placed, anchor
                )
            except Exception as exc:
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {exc}")
                if pause_on_error:
                    await pause_for_inspection(page, "render", index, kind, exc)
                raise
            bar.update(1)
    finally:
        bar.close()

    await page.close()
    await context.close()

    webm = Path(await video.path())
    total = probe_duration(webm)
    bed = work / "bed.wav"
    build_audio_bed(placed, total, bed)
    mux(webm, bed, out_mp4)


async def _render_step(
    page: Page,
    recorder: Recorder,
    overlay: Overlay,
    scenario: Scenario,
    step: Step,
    kind: str,
    index: int,
    segments: dict[int, Segment],
    placed: list[Placed],
    anchor: float,
) -> None:
    seg = segments.get(index)
    if seg is not None:
        placed.append(Placed(segment=seg, offset=time.monotonic() - anchor))
        await asyncio.sleep(seg.duration)  # narracja steruje tempem

    if kind == "say":
        return
    if kind == "navigate":
        await recorder.navigate(_resolve_url(scenario, step.navigate))
        await overlay.ensure(page)
        return
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return

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
