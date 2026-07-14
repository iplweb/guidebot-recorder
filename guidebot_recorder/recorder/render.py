"""The `render` phase — deterministic replay + film assembly (§8/§9).

Phase 0: pre-synthesize all narration into the cache (no "live" TTS calls).
Render: 0×LLM, fresh browser, single pass; narration drives the pace.
Assembly: Playwright video + audio bed (ffmpeg), approximate sync (decision K2).

Resolved actions are read from the separate ``*.compiled.yaml`` sidecar.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Browser, Page
from tqdm import tqdm

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.tts.base import Segment, TtsCache, TtsProvider
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import mux, probe_duration


class RenderError(RuntimeError):
    """A step needs (re-)compile: missing action or mismatched identity."""


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

    scenario = load_scenario(path)
    cfg = scenario.config

    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise RenderError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if len(compiled.actions) != len(scenario.steps):
        raise RenderError("compiled niezgodny z liczbą kroków — uruchom `compile`")

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
    overlay = Overlay(cfg.cursor)
    await overlay.install(page)
    chrome = Chrome(cfg.chrome) if cfg.chrome.enabled else None
    if chrome is not None:
        await chrome.install(page)
    recorder = Recorder(page, overlay, settle_ms=cfg.cursor.settle)

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
                    page,
                    recorder,
                    overlay,
                    chrome,
                    scenario,
                    step,
                    kind,
                    index,
                    compiled.actions[index],
                    segments,
                    placed,
                    anchor,
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
    chrome: Chrome | None,
    scenario: Scenario,
    step: Step,
    kind: str,
    index: int,
    cached: CachedAction | None,
    segments: dict[int, Segment],
    placed: list[Placed],
    anchor: float,
) -> None:
    # Both visual layers can be removed by an SPA without a navigation.  Check
    # them before every recorded step, including narration-only and timed waits.
    await overlay.ensure(page)
    if chrome is not None:
        await chrome.ensure(page)

    seg = segments.get(index)
    if seg is not None:
        placed.append(Placed(segment=seg, offset=time.monotonic() - anchor))
        await asyncio.sleep(seg.duration)  # narration drives the pace

    if kind == "say":
        return
    if kind == "navigate":
        source_url = step.navigate_url()
        assert source_url is not None  # guaranteed by command_kind()
        url = _resolve_url(scenario, source_url)
        type_override = step.navigate_type_override()
        animate = (
            scenario.config.chrome.type_on_navigate
            if type_override is None
            else type_override
        )
        if chrome is not None and scenario.config.chrome.show_url and animate:
            await chrome.set_url(page, url, animate=True)

        await recorder.navigate(url)

        # An instant update happens after goto so redirects are reflected.  The
        # animated variant is typed before goto, then ensure synchronizes the
        # final page URL after the new document has loaded.
        if chrome is not None and scenario.config.chrome.show_url and not animate:
            await chrome.set_url(page, page.url, animate=False)
        await overlay.ensure(page)
        if chrome is not None:
            await chrome.ensure(page)
        return
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return

    if cached is None:
        raise RenderError(f"krok {index}: brak cachedAction — uruchom `compile`")
    if cached.action != "waitFor" and not await reuse_is_valid(page, cached):
        raise RenderError(f"krok {index}: niezgodna tożsamość — uruchom `compile`")

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
