"""The `render` phase — deterministic replay + film assembly (§8/§9).

Phase 0: pre-synthesize all narration into the cache (no "live" TTS calls).
Render: 0×LLM, fresh browser, single pass; narration drives the pace.
Assembly: Playwright video + audio bed (ffmpeg), approximate sync (decision K2).

Resolved actions are read from the separate ``*.compiled.yaml`` sidecar.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    Video,
)
from tqdm import tqdm

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.tts.base import Segment, TtsCache, TtsProvider
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import (
    compose_popup_video,
    mux,
    mux_preencoded,
    probe_duration,
)


class RenderError(RuntimeError):
    """A step needs (re-)compile: missing action or mismatched identity."""


@dataclass(slots=True)
class _PageObservation:
    opened_at: float
    video: Video | None
    closed_at: float | None = None


@dataclass(slots=True)
class _PopupSession:
    page: Page
    video: Video
    opened_at: float
    closed_at: float | None = None
    close_handled: bool = False


def _active_page(main_page: Page, popup: _PopupSession | None) -> Page:
    if main_page.is_closed():
        raise RenderError("główne okno zostało zamknięte podczas render")
    if popup is not None and not popup.page.is_closed():
        return popup.page
    return main_page


def _unexpected_pages(
    observed_pages: dict[Page, _PageObservation],
    main_page: Page,
    popup: _PopupSession | None,
) -> list[Page]:
    """Observed pages outside the deterministic main + one-popup contract.

    The event-backed list deliberately retains pages that already closed, so an
    unexpected page cannot evade validation by opening and closing between steps.
    """

    expected_popup = popup.page if popup is not None else None
    return [page for page in observed_pages if page is not main_page and page is not expected_popup]


def _sync_popup_close(
    popup: _PopupSession | None,
    observed_pages: dict[Page, _PageObservation],
    anchor: float,
) -> None:
    if popup is None or popup.closed_at is not None:
        return
    observation = observed_pages.get(popup.page)
    if observation is not None and observation.closed_at is not None:
        popup.closed_at = max(popup.opened_at, observation.closed_at - anchor)


async def _ensure_visuals(page: Page, overlay: Overlay, chrome: Chrome | None) -> None:
    """Restore both DOM overlays on the currently recorded page."""

    await overlay.ensure(page)
    if chrome is not None:
        await chrome.ensure(page)


async def _prepare_main_after_popup_close(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
    settle_ms: float,
) -> None:
    """Let opener navigation settle before touching its execution context again."""

    await page.bring_to_front()
    await Recorder(page, None, settle_ms=settle_ms).apply_readiness("none")
    try:
        await page.wait_for_load_state()
        await _ensure_visuals(page, overlay, chrome)
    except PlaywrightError as exc:
        if page.is_closed():
            raise RenderError("główne okno zamknęło się po zamknięciu popupu") from exc
        # A navigation can destroy the context between the load-state check and
        # cursor restoration. Wait for the replacement document and retry once.
        await page.wait_for_load_state()
        await _ensure_visuals(page, overlay, chrome)


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


def _compiled_from(step: Step) -> str:
    kind = step.command_kind()
    if kind == "teach":
        return step.teach
    if kind == "click":
        return step.click
    if kind == "hover":
        return step.hover
    if kind == "enterText":
        return step.enter_text.into
    if kind == "wait" and isinstance(step.wait, WaitUntil):
        return step.wait.until
    raise ValueError(f"krok {kind} nie wymaga cachedAction")


def _compiled_action_is_current(
    step: Step, action: CachedAction | None, scenario_hash: str
) -> bool:
    """Check source/config fingerprints before replaying frozen behavior."""

    if not step.requires_target():
        return action is None
    if action is None:
        return False
    kind = step.command_kind()
    expected_action = {
        "click": "click",
        "hover": "hover",
        "enterText": "type",
        "wait": "waitFor",
    }.get(kind)
    if expected_action is not None and action.action != expected_action:
        return False
    expected_state = step.wait.state if isinstance(step.wait, WaitUntil) else None
    fingerprint = action.fingerprint
    if not (
        fingerprint.compiler_version == COMPILER_VERSION
        and fingerprint.command_kind == kind
        and fingerprint.compiled_from == _compiled_from(step)
        and fingerprint.config_hash == scenario_hash
        and fingerprint.state == expected_state
        and fingerprint.expect == action.expect
    ):
        return False
    return not (
        kind == "teach"
        and action.action == "type"
        and (action.input_text is None or action.input_text not in step.teach)
    )


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
    if compiled.compiler_version != COMPILER_VERSION or any(
        action is not None and action.fingerprint.compiler_version != COMPILER_VERSION
        for action in compiled.actions
    ):
        raise RenderError("compiled ma starszą wersję — uruchom `compile`")
    scenario_hash = config_hash(cfg)
    for index, (step, action) in enumerate(zip(scenario.steps, compiled.actions, strict=True)):
        if not _compiled_action_is_current(step, action, scenario_hash):
            raise RenderError(f"krok {index}: compiled jest nieaktualny — uruchom `compile`")

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
    overlay = Overlay(cfg.cursor)
    await overlay.install_context(context)

    observed_pages: dict[Page, _PageObservation] = {}

    def observe_page(candidate: Page) -> None:
        if candidate in observed_pages:
            return
        observation = _PageObservation(
            opened_at=time.monotonic(),
            video=candidate.video,
        )
        observed_pages[candidate] = observation

        def mark_closed(_: Page, observed: _PageObservation = observation) -> None:
            if observed.closed_at is None:
                observed.closed_at = time.monotonic()

        candidate.on("close", mark_closed)

    context.on("page", observe_page)
    page = await context.new_page()
    observe_page(page)
    page.set_default_timeout(timeout * 1000)
    video = page.video
    if video is None:  # pragma: no cover - record_video_dir makes this invariant true
        await context.close()
        raise RenderError("Playwright nie udostępnił nagrania głównego okna")

    chrome = Chrome(cfg.chrome) if cfg.chrome.enabled else None
    if chrome is not None:
        await chrome.install(page)

    # Chromium's screencast may not emit a first frame for a pristine about:blank
    # page.  A scenario can narrate for several seconds before its first navigate;
    # anchoring at the Page event would then put that narration on a timeline the
    # WebM never encoded.  Paint a neutral document, force one captured frame, and
    # only then establish the shared narration/window clock.  The tiny warm-up is
    # bounded pre-roll; it avoids losing an arbitrarily long opening narration.
    await page.set_content("<style>html,body{margin:0;background:white}</style>")
    await _ensure_visuals(page, overlay, chrome)
    await page.screenshot()
    await page.wait_for_timeout(100)
    anchor = time.monotonic()

    placed: list[Placed] = []
    popup: _PopupSession | None = None
    popup_open_at_end = False

    bar = tqdm(total=len(scenario.steps), desc="render", unit="krok", disable=not verbose)
    try:
        for index, step in enumerate(scenario.steps):
            _sync_popup_close(popup, observed_pages, anchor)
            if popup is not None and popup.page.is_closed() and not popup.close_handled:
                raise RenderError("popup zamknął się poza obsługiwaną akcją scenariusza")
            if _unexpected_pages(observed_pages, page, popup):
                raise RenderError(f"krok {index}: nieoczekiwany popup — uruchom `compile --force`")
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(scenario.steps)}] {kind}")

            active_page = _active_page(page, popup)
            await active_page.bring_to_front()
            await _ensure_visuals(active_page, overlay, chrome)

            seg = segments.get(index)
            if seg is not None:
                placed.append(Placed(segment=seg, offset=time.monotonic() - anchor))
                await asyncio.sleep(seg.duration)  # narration drives the pace

            _sync_popup_close(popup, observed_pages, anchor)
            if popup is not None and popup.page.is_closed() and not popup.close_handled:
                raise RenderError("popup zamknął się asynchronicznie podczas narracji")
            active_page = _active_page(page, popup)
            if _unexpected_pages(observed_pages, page, popup):
                raise RenderError(f"krok {index}: nieoczekiwany popup — uruchom `compile --force`")
            await active_page.bring_to_front()
            await _ensure_visuals(active_page, overlay, chrome)
            cached = compiled.actions[index]
            if cached is not None and cached.opens_popup and popup is not None:
                raise RenderError("v1 obsługuje co najwyżej jeden popup w całej sesji")
            recorder = Recorder(active_page, overlay, settle_ms=cfg.cursor.settle)
            try:
                opened = await _render_step(
                    active_page,
                    recorder,
                    overlay,
                    chrome,
                    scenario,
                    step,
                    kind,
                    index,
                    cached,
                    anchor,
                    observed_pages,
                )
                if opened is not None:
                    popup = opened
                    popup.page.set_default_timeout(timeout * 1000)
                    prepared = await _prepare_popup(popup.page, overlay, chrome)
                    _sync_popup_close(popup, observed_pages, anchor)
                    if not prepared:
                        raise RenderError("popup zamknął się podczas otwierania")
                if page.is_closed():
                    raise RenderError("główne okno zostało zamknięte podczas render")
                _sync_popup_close(popup, observed_pages, anchor)
                if popup is not None and popup.page.is_closed():
                    if not popup.close_handled:
                        if opened is not None or kind in {"say", "navigate", "wait"}:
                            raise RenderError(
                                "popup zamknął się asynchronicznie poza obsługiwaną akcją"
                            )
                        popup.close_handled = True
                        await _prepare_main_after_popup_close(
                            page,
                            overlay,
                            chrome,
                            cfg.cursor.settle,
                        )
                if _unexpected_pages(observed_pages, page, popup):
                    raise RenderError(
                        f"krok {index}: nieoczekiwany popup — uruchom `compile --force`"
                    )
            except Exception as exc:
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {exc}")
                if pause_on_error:
                    debug_page = _active_page(page, popup)
                    await pause_for_inspection(debug_page, "render", index, kind, exc)
                raise
            bar.update(1)
        await asyncio.sleep(0)
        _sync_popup_close(popup, observed_pages, anchor)
        if popup is not None and popup.page.is_closed() and not popup.close_handled:
            raise RenderError("popup zamknął się asynchronicznie na końcu scenariusza")
    finally:
        bar.close()
        _sync_popup_close(popup, observed_pages, anchor)
        if popup is not None and popup.closed_at is None:
            popup_open_at_end = True
            popup.closed_at = max(popup.opened_at, time.monotonic() - anchor)
        await context.close()

    main_webm = Path(await video.path())
    bed = work / "bed.wav"
    if popup is None:
        total = probe_duration(main_webm)
        build_audio_bed(placed, total, bed)
        mux(main_webm, bed, out_mp4)
        return

    popup_webm = Path(await popup.video.path())
    closed_at = probe_duration(main_webm) if popup_open_at_end else popup.closed_at
    assert closed_at is not None
    composite = work / f"{out_mp4.stem}.composite.mp4"
    compose_popup_video(main_webm, popup_webm, composite, popup.opened_at, closed_at)
    total = probe_duration(composite)
    build_audio_bed(placed, total, bed)
    mux_preencoded(composite, bed, out_mp4)


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
    anchor: float,
    observed_pages: dict[Page, _PageObservation],
) -> _PopupSession | None:
    # Both visual layers can be removed by an SPA without a navigation.  Check
    # them before every recorded step, including narration-only and timed waits.
    await _ensure_visuals(page, overlay, chrome)

    if kind == "say":
        return None
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
        await _ensure_visuals(page, overlay, chrome)
        return None
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return None

    if cached is None:
        raise RenderError(f"krok {index}: brak cachedAction — uruchom `compile`")
    if cached.action != "waitFor" and not await reuse_is_valid(page, cached):
        raise RenderError(f"krok {index}: niezgodna tożsamość — uruchom `compile --force`")
    if cached.opens_popup and cached.action != "click":
        raise RenderError(f"krok {index}: tylko click może otworzyć popup")

    opened: _PopupSession | None = None
    if cached.action == "click":
        if cached.opens_popup:
            try:
                async with page.expect_popup() as popup_info:
                    await recorder.click(cached.target)
                popup_page = await popup_info.value
            except PlaywrightTimeoutError as exc:
                raise RenderError(
                    f"krok {index}: oczekiwany popup nie otworzył się — uruchom `compile --force`"
                ) from exc

            observation = observed_pages.get(popup_page)
            if observation is None:  # defensive fallback; context event is the primary path
                observation = _PageObservation(
                    opened_at=time.monotonic(),
                    video=popup_page.video,
                    closed_at=time.monotonic() if popup_page.is_closed() else None,
                )
                observed_pages[popup_page] = observation
            popup_video = observation.video or popup_page.video
            if popup_video is None:  # pragma: no cover - context recording is enabled
                raise RenderError("Playwright nie udostępnił nagrania popupu")
            opened_at = max(0.0, observation.opened_at - anchor)
            closed_at = (
                max(opened_at, observation.closed_at - anchor)
                if observation.closed_at is not None
                else None
            )
            opened = _PopupSession(
                page=popup_page,
                video=popup_video,
                opened_at=opened_at,
                closed_at=closed_at,
            )
        else:
            await recorder.click(cached.target)
    elif cached.action == "hover":
        await recorder.hover(cached.target)
    elif cached.action == "type":
        input_text = step.enter_text.text if step.enter_text is not None else cached.input_text
        if input_text is None:
            raise RenderError(f"krok {index}: brak zamrożonego tekstu — uruchom `compile`")
        await recorder.enter_text(cached.target, input_text)
    elif cached.action == "waitFor":
        timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        await recorder.wait_for(cached.target, cached.state or "visible", timeout)
    if not page.is_closed():
        await recorder.apply_readiness(cached.expect)
    return opened


async def _prepare_popup(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
) -> bool:
    """Prepare a new page; translate close races into lifecycle state."""

    if page.is_closed():
        return False
    try:
        await page.bring_to_front()
        await page.wait_for_load_state()
        await overlay.ensure(page)
        if chrome is not None:
            await chrome.install(page)
    except PlaywrightError:
        if page.is_closed():
            return False
        raise
    return not page.is_closed()
