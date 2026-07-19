"""The `render` phase — deterministic replay + film assembly (§8/§9).

Phase 0: pre-synthesize every configured narration track into the cache.
Render: 0×LLM, fresh browser, single pass; narration drives the pace.
Assembly: Playwright video + language audio beds (ffmpeg), approximate sync (K2).

Resolved actions are read from the separate ``*.compiled.yaml`` sidecar.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    Frame,
    Page,
    Video,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from tqdm import tqdm

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction
from guidebot_recorder.models.config import ChromeConfig, SoundConfig, TtsConfig, config_hash
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import (
    pause_for_inspection,
    redact_exception,
    scenario_sensitive_values,
)
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.slide import SlideOverlay
from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    TtsProvider,
    cache_key,
)
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import (
    MuxAudioTrack,
    compose_popup_video,
    mux_audio_tracks,
    probe_duration,
)
from guidebot_recorder.video.sfx import build_sfx_bed, mix_sfx_into_bed

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1
_VIDEO_POSTROLL_SECONDS = 0.1
_TTS_CONCURRENCY = 8
# Each worker can own a full ffmpeg process. Keep the pool below both the host's
# CPU count and a conservative process ceiling instead of scaling with languages.
_AUDIO_BED_CONCURRENCY = max(1, min(4, os.cpu_count() or 1))


class RenderError(RuntimeError):
    """A step needs (re-)compile: missing action or mismatched identity."""


#: A slide card's on-screen content, as consumed by ``SlideOverlay.show``/``.ensure``.
Card = dict[str, str | None]


@dataclass(slots=True)
class _PageObservation:
    opened_at: float
    video: Video | None
    closed_at: float | None = None
    visual_prime: asyncio.Task[float | None] | None = None


@dataclass(slots=True)
class _PopupSession:
    page: Page
    video: Video
    opened_at: float
    visual_ready_delay: float = 0.0
    closed_at: float | None = None
    close_handled: bool = False


@dataclass(slots=True)
class _TtsWork:
    text: str
    config: TtsConfig
    destinations: list[tuple[str, int]]


def _active_page(main_page: Page, popup: _PopupSession | None) -> Page:
    if main_page.is_closed():
        raise RenderError("główne okno zostało zamknięte podczas render")
    if popup is not None and not popup.page.is_closed():
        return popup.page
    return main_page


def _expect_chrome(chrome: Chrome | None, bare_popups: bool) -> bool:
    """Whether the legacy in-DOM chrome bar (``[data-guidebot-chrome]``) is expected.

    The bar is a context-wide init script, so ``bare_popups`` (floating) cannot
    suppress it on the popup alone — it suppresses it on *every* top-level
    non-shell document, including the main window's transient ``about:blank``
    warm-up before it becomes the shell. So the legacy bar is expected only when
    chrome is enabled and popups are not bare. The main window's real chrome is
    the shell (``install_shell`` / the shell branch of :func:`_ensure_visuals`),
    which is independent of this flag; the cursor overlay is always expected.
    """

    return chrome is not None and not bare_popups


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


async def _wait_for_render_popup(
    observed_pages: dict[Page, _PageObservation],
    known_pages: set[Page],
    started_at: float,
    timeout: float = _POPUP_DETECTION_SECONDS,
) -> list[Page]:
    """Return pages opened inside the actual-click discovery window."""

    deadline = started_at + timeout
    while True:
        candidates = [
            page
            for page, observation in observed_pages.items()
            if page not in known_pages and started_at <= observation.opened_at <= deadline
        ]
        if candidates:
            await asyncio.sleep(_POPUP_QUIESCENCE_SECONDS)
            return sorted(
                (
                    page
                    for page, observation in observed_pages.items()
                    if page not in known_pages and started_at <= observation.opened_at <= deadline
                ),
                key=lambda page: observed_pages[page].opened_at,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.05, remaining))


def navigate_pill_mode(chrome: ChromeConfig, type_override: bool | None) -> str:
    """Select the main-window address-bar behavior for a ``navigate`` step.

    Returns one of ``"choreograph"`` (pointer → click → focus → natural type),
    ``"type"`` (typed pill only, no pointer), or ``"instant"`` (no typing). The
    caller still gates on ``chrome.enabled`` and ``show_url``.
    """

    animate = chrome.type_on_navigate if type_override is None else type_override
    if not animate:
        return "instant"
    return "choreograph" if chrome.interact_on_navigate else "type"


def _is_shell_page(page: Page) -> bool:
    """True when ``page`` is the main-window shell (served from the sentinel origin)."""

    return page.url.startswith(SHELL_URL)


async def _ensure_visuals(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
    *,
    expect_chrome: bool | None = None,
) -> None:
    """Restore both DOM overlays in one browser task to avoid a partial frame.

    In the shell (main render window) the invariant is reworded: the site iframe
    and the shell bar live in the shell document (the framed site can no longer
    touch them), and the cursor is restored on the shell page. The pill URL is
    deliberately *not* resynced here — that would flip it to the shell sentinel
    URL; the pill is sourced from the site frame only on navigate steps.

    ``expect_chrome`` defaults to ``chrome is not None``; pass ``False`` for a
    bare (floating) popup so the chrome bar/API is not demanded or asserted while
    the cursor is still ensured.
    """

    if expect_chrome is None:
        expect_chrome = chrome is not None

    if chrome is not None and _is_shell_page(page):
        await chrome.ensure_shell(page)
        await overlay.ensure(page)
        return

    # The common path checks controller readiness and mounts both layers in one
    # browser task. A missing controller returns without painting; after Python
    # reinjects it, the same task is rerun in strict mode against the current
    # document. This retains page-replacement safety without paying two evaluate
    # round-trips when the init scripts are already alive.
    ensure_script = """async ([x, y, expectChrome, url, strict]) => {
            const cursor = window.__guidebot_cursor;
            const chrome = window.__guidebot_chrome;
            const cursorReady = !!cursor && ["ensure", "moveTo"].every(
                name => typeof cursor[name] === "function"
            );
            const chromeReady = !expectChrome || (
                !!chrome && ["ensure", "setUrl"].every(
                    name => typeof chrome[name] === "function"
                )
            );
            if (!cursorReady || !chromeReady) {
                if (strict) {
                    if (!cursorReady) {
                        throw new Error("guidebot cursor API is unavailable after injection");
                    }
                    throw new Error("guidebot chrome API is unavailable after injection");
                }
                return {cursor: cursorReady, chrome: chromeReady, mounted: false};
            }
            if (expectChrome) {
                chrome.ensure(url);
            }
            cursor.ensure();
            await cursor.moveTo(x, y, 0);
            return {cursor: true, chrome: true, mounted: true};
        }"""
    args = [overlay.pos[0], overlay.pos[1], expect_chrome, page.url, False]
    readiness = await page.evaluate(
        ensure_script,
        args,
    )
    if readiness.get("mounted"):
        return
    # Context init scripts normally make both APIs available. Repair a missing
    # controller first; the retry still mounts both layers atomically.
    if chrome is not None and expect_chrome and not readiness.get("chrome"):
        await chrome.ensure(page)
    if not readiness.get("cursor"):
        await overlay.ensure(page)
    # A repair can race a navigation/document replacement. Read the URL again
    # for the strict mount instead of reusing the pre-repair snapshot.
    args[3] = page.url
    args[-1] = True
    await page.evaluate(ensure_script, args)


async def _prime_visuals(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
    *,
    expect_chrome: bool | None = None,
    timeout: float = _POPUP_DETECTION_SECONDS,
) -> float | None:
    """Mount visual layers from the page event, before its first useful frame.

    Chromium can replace a freshly opened ``about:blank`` document without
    rerunning init-script timers. Keep priming until the document root and both
    layers stay stable for one quiescence window, then force a captured frame.

    ``expect_chrome`` defaults to ``chrome is not None``; pass ``False`` for a
    bare (floating) popup so the prime loop does not wait for a
    ``[data-guidebot-chrome]`` bar that never mounts (the cursor is still
    required).
    """

    if expect_chrome is None:
        expect_chrome = chrome is not None

    deadline = time.monotonic() + timeout
    marker = f"{time.monotonic_ns()}-{id(page)}"
    stable_since: float | None = None
    status_script = """([token, expectChrome]) => {
        const root = document.documentElement;
        if (!root) return {ready: false};
        const sameRoot = root.__guidebotVisualPrime === token;
        root.__guidebotVisualPrime = token;
        return {
            ready: true,
            sameRoot,
            cursor: !!document.querySelector("[data-guidebot-cursor]"),
            chrome: !expectChrome || !!document.querySelector("[data-guidebot-chrome]"),
        };
    }"""
    while not page.is_closed():
        try:
            status = await page.evaluate(status_script, [marker, expect_chrome])
            now = time.monotonic()
            complete = (
                isinstance(status, dict)
                and status.get("ready") is True
                and status.get("sameRoot") is True
                and status.get("cursor") is True
                and status.get("chrome") is True
            )
            if not complete:
                await _ensure_visuals(page, overlay, chrome, expect_chrome=expect_chrome)
                stable_since = now
            elif stable_since is None:
                stable_since = now
            elif now - stable_since >= _POPUP_QUIESCENCE_SECONDS:
                await page.screenshot()
                final_status = await page.evaluate(status_script, [marker, expect_chrome])
                if (
                    isinstance(final_status, dict)
                    and final_status.get("sameRoot") is True
                    and final_status.get("cursor") is True
                    and final_status.get("chrome") is True
                ):
                    return time.monotonic()
                stable_since = None
        except PlaywrightError:
            # A navigation may replace the execution context between the page
            # event and injection. Retry only inside the bounded prime window.
            if page.is_closed():
                return None
            stable_since = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RenderError("nie udało się zainicjować warstw wizualnych nowej strony")
        await asyncio.sleep(min(0.01, remaining))
    return None


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
    return step.narration()


async def _wait_for_step_narration(segments: list[Segment]) -> None:
    """Pace one shared visual step by its longest configured narration."""

    if segments:
        await asyncio.sleep(max(segment.duration for segment in segments))


async def _presynthesize_narration(
    steps: Sequence[Step],
    configs: list[TtsConfig],
    cache: TtsCache,
    provider: TtsProvider,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, dict[int, Segment]]:
    """Synthesize unique cache entries concurrently and map them to every step.

    Repeated narration can resolve to the same on-disk cache path. Deduplicating
    by the canonical cache key before scheduling prevents concurrent writers to
    that path and avoids duplicate provider calls on a cold cache.
    """

    segments: dict[str, dict[int, Segment]] = {tts.lang: {} for tts in configs}
    by_key: dict[str, _TtsWork] = {}
    for index, step in enumerate(steps):
        canonical_text = _narration(step)
        if canonical_text is None:
            continue
        for track_index, tts in enumerate(configs):
            text = canonical_text if track_index == 0 else step.translations[tts.lang]
            key = cache_key(
                text,
                tts,
                provider.adapter_version,
                CACHE_SCHEMA_VERSION,
            )
            work = by_key.get(key)
            if work is None:
                work = _TtsWork(text=text, config=tts, destinations=[])
                by_key[key] = work
            work.destinations.append((tts.lang, index))

    semaphore = asyncio.Semaphore(_TTS_CONCURRENCY)

    async def synthesize(work: _TtsWork) -> None:
        async with semaphore:
            segment = await cache.get_or_synth(work.text, work.config, provider)
        for language, index in work.destinations:
            segments[language][index] = segment
        if on_progress is not None:
            on_progress(len(work.destinations))

    results = await asyncio.gather(
        *(synthesize(work) for work in by_key.values()),
        return_exceptions=True,
    )
    # Wait for every started cache writer before propagating an error; otherwise
    # a sibling task could keep writing after Phase 0 has already returned.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return segments


async def _mux_tracks_for_timeline(
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    *,
    sfx_bed: Path | None = None,
) -> list[MuxAudioTrack]:
    """Build one full-length bed per language in deterministic stream order.

    When *sfx_bed* is set, narration is rendered to a temp name first, then the
    shared SFX bed is mixed into the final `bed-<lang>.wav` so ``bed-*.wav`` keeps
    naming ``_publish_render_artifacts`` relies on.
    """

    for tts in configs:
        for placement in placed_by_language[tts.lang]:
            if placement.offset + placement.segment.duration > total:
                raise RenderError(
                    f"narracja {tts.lang} wykracza poza nagranie wideo — render przerwany"
                )

    semaphore = asyncio.Semaphore(_AUDIO_BED_CONCURRENCY)

    def build_track(index: int, tts: TtsConfig) -> MuxAudioTrack:
        bed = work / f"bed-{tts.mp4_language()}.wav"
        if sfx_bed is not None:
            narr = work / f"narr-{tts.mp4_language()}.wav"
            build_audio_bed(placed_by_language[tts.lang], total, narr)
            mix_sfx_into_bed(narr, sfx_bed, bed, total)  # bed = narration + SFX
        else:
            build_audio_bed(placed_by_language[tts.lang], total, bed)
        return MuxAudioTrack(
            path=bed,
            language=tts.mp4_language(),
            title=tts.title or tts.lang,
            default=index == 0,
        )

    async def build_bounded(index: int, tts: TtsConfig) -> MuxAudioTrack:
        async with semaphore:
            worker = asyncio.create_task(asyncio.to_thread(build_track, index, tts))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an asyncio wrapper cannot stop a running thread (or
                # its ffmpeg child). Keep the staging directory alive until that
                # worker has actually returned, with caller cancellation primary.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                if not worker.cancelled():
                    try:
                        worker.result()
                    except BaseException:
                        pass
                raise

    tasks = [asyncio.create_task(build_bounded(index, tts)) for index, tts in enumerate(configs)]
    gathered = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(gathered)
    except asyncio.CancelledError:
        # Do not start queued ffmpeg work after cancellation, but let workers
        # already inside to_thread finish before TemporaryDirectory can unwind.
        for task in tasks:
            task.cancel()
        while not gathered.done():
            try:
                await asyncio.shield(gathered)
            except asyncio.CancelledError:
                continue
        if not gathered.cancelled():
            gathered.result()
        raise
    tracks: list[MuxAudioTrack] = []
    # gather preserves config order. It also waits for all ffmpeg workers before
    # an error leaves the staging directory, avoiding writes into deleted paths.
    for result in results:
        if isinstance(result, BaseException):
            raise result
        tracks.append(result)
    return tracks


def _publish_render_artifacts(
    staged_mp4: Path,
    tracks: list[MuxAudioTrack],
    work: Path,
    out_mp4: Path,
) -> None:
    """Commit the new master and complete bed set, rolling back publish errors."""

    backup = Path(tempfile.mkdtemp(prefix=".audio-beds-backup-", dir=work))
    published: list[Path] = []
    try:
        for current in list(work.glob("bed-*.wav")):
            os.replace(current, backup / current.name)
        for track in tracks:
            destination = work / track.path.name
            os.replace(track.path, destination)
            published.append(destination)
        # The master is the commit point: until this atomic replace succeeds, the
        # previous MP4 remains in place and any bed publication error is rolled back.
        os.replace(staged_mp4, out_mp4)
    except BaseException:
        for destination in published:
            destination.unlink(missing_ok=True)
        for previous in backup.glob("bed-*.wav"):
            os.replace(previous, work / previous.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


async def _assemble_audio_tracks(
    video: Path,
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    out_mp4: Path,
    *,
    preencoded: bool = False,
    sound: SoundConfig | None = None,
    sfx_offsets: list[tuple[str, float]] | None = None,
) -> None:
    """Stage a complete bed set, mux atomically, then publish durable WAVs.

    When *sound* is enabled and *sfx_offsets* is non-empty, the shared SFX bed is
    built ONCE in staging (from the packaged click/key assets) and mixed into every
    language's narration bed via `_mux_tracks_for_timeline`.
    """

    with tempfile.TemporaryDirectory(prefix=".audio-beds-", dir=work) as staging:
        staged_mp4 = Path(staging) / f"{out_mp4.stem}.mp4"
        sfx_bed = None
        if sound is not None and sound.enabled and sfx_offsets:
            sfx_bed = Path(staging) / "sfx-bed.wav"
            sfx_pkg = files("guidebot_recorder.sfx")
            with (
                as_file(sfx_pkg.joinpath("click.wav")) as cp,
                as_file(sfx_pkg.joinpath("key.wav")) as kp,
            ):
                build_sfx_bed(
                    sfx_offsets,
                    total,
                    sfx_bed,
                    click_path=Path(cp),
                    key_path=Path(kp),
                    gain_db=sound.volume,
                )
        tracks = await _mux_tracks_for_timeline(
            configs,
            placed_by_language,
            total,
            Path(staging),
            sfx_bed=sfx_bed,
        )
        mux_audio_tracks(
            video,
            tracks,
            staged_mp4,
            preencoded=preencoded,
            video_duration=total,
        )
        _publish_render_artifacts(staged_mp4, tracks, work, out_mp4)


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
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> None:
    path = Path(path)
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    scenario = load_scenario(path, env)
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cfg = scenario.config
    audio_configs = [cfg.tts, *cfg.audio_tracks]
    providers = {tts.provider for tts in audio_configs}
    if len(providers) != 1:
        raise RenderError(
            "jeden render obsługuje obecnie jeden provider TTS; "
            f"skonfigurowano: {', '.join(sorted(providers))}"
        )

    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise RenderError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if compiled.source != path.name:
        raise RenderError(
            f"compiled pochodzi z innego scenariusza ({compiled.source}) — uruchom `compile`"
        )
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
    narration_count = sum(_narration(step) is not None for step in scenario.steps)
    presynth = tqdm(
        total=narration_count * len(audio_configs),
        desc="tts",
        unit="segment",
        disable=not verbose,
    )
    try:
        segments = await _presynthesize_narration(
            scenario.steps,
            audio_configs,
            cache,
            tts_provider,
            on_progress=presynth.update,
        )
    finally:
        presynth.close()

    # --- Render z nagrywaniem wideo (viewport z config — patrz compile) ---
    work = out_mp4.parent / ".guidebot_video" / out_mp4.stem
    work.mkdir(parents=True, exist_ok=True)
    # The context viewport and video size stay at the configured dimensions so the
    # output MP4 keeps its size and popups are geometrically untouched; the shell
    # shrinks only the site iframe interior (see compile / site_viewport).
    context = await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        record_video_dir=str(work),
        record_video_size={"width": cfg.viewport.width, "height": cfg.viewport.height},
        **({"bypass_csp": True, "service_workers": "block"} if cfg.chrome.enabled else {}),
    )
    overlay = Overlay(cfg.cursor, cfg.viewport)
    # Role-gating contract: cursor.js and slide.js MUST be registered before
    # chrome.js. Inside the site iframe, both rely on reading the real
    # ``window.top`` to bail (cursor.js to skip mounting a duplicate cursor,
    # slide.js's ``isTop`` guard to skip installing ``window.__guidebot_slide``);
    # chrome.js is what shadows ``top`` (frame-bust neutralization). If any of
    # these init scripts ran after chrome.js, it would read the shadowed
    # ``top``, misidentify as the top window, and mount inside the frame.
    await overlay.install_context(context)
    slide = SlideOverlay()
    await slide.install_context(context)
    # Composited popups (float or slide) render bare (no in-DOM chrome bar); the
    # compositor frames them in post. This flips the chrome.js popup-site branch
    # off and gates the fail-loud "expect chrome" checks on popup pages below.
    bare_popups = cfg.popup.is_bare
    chrome = Chrome(cfg.chrome, bare_popups=bare_popups) if cfg.chrome.enabled else None
    if chrome is not None:
        await chrome.install_context(context)
        # Strip X-Frame-Options / CSP frame-ancestors so arbitrary sites frame.
        await install_framing(context, shell_origin=SHELL_URL)

    # --- Slide card state -----------------------------------------------------
    # `card_active`/`active_card` track whether a slide card currently owns the
    # screen (painted either by a `slide` step or the auto-intro below). When no
    # card is ever painted (no `slide` steps, `intro.enabled=False`), these stay
    # False/None for the whole render and every helper below is a pure pass-
    # through to today's `_ensure_visuals` — i.e. byte-identical back-compat.
    card_active = False
    active_card: Card | None = None

    async def _chrome_hide(pg: Page) -> None:
        if chrome is not None:
            await chrome.hide(pg)

    async def _chrome_show(pg: Page) -> None:
        if chrome is not None:
            await chrome.show(pg)

    async def _assert_card_alive(pg: Page) -> None:
        """Fail loud when a navigation destroyed the card mid-say.

        A fresh, tokenless document (``slide.token`` falsy) means the picture
        on screen is no longer the card the narration/scenario describes —
        never narrate over — or silently dismiss — the wrong picture.
        """
        if not await slide.token(pg):
            raise RenderError("karta slajdu zniknęła po nawigacji — narracja nad złym obrazem")

    async def _ensure_card(pg: Page) -> None:
        """Card-aware replacement for `_ensure_visuals`: re-mount the active
        card (rebuild-from-missing only; a live card's content is untouched)
        and re-assert the hidden cursor/chrome layers.
        """
        await _assert_card_alive(pg)
        assert active_card is not None  # guaranteed by the card_active invariant
        await slide.ensure(pg, active_card)
        await overlay.hide(pg)
        await _chrome_hide(pg)

    observed_pages: dict[Page, _PageObservation] = {}

    def observe_page(candidate: Page) -> None:
        if candidate in observed_pages:
            return
        # Bare (floating) popups carry no legacy chrome bar; nor does the main
        # window's about:blank warm-up under that flag. Prime against the cursor
        # only, or the prime loop deadlocks waiting for a bar that never mounts.
        expect_chrome = _expect_chrome(chrome, bare_popups)
        observation = _PageObservation(
            opened_at=time.monotonic(),
            video=candidate.video,
            visual_prime=asyncio.create_task(
                _prime_visuals(candidate, overlay, chrome, expect_chrome=expect_chrome)
            ),
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
    main_observation = observed_pages[page]
    if main_observation.visual_prime is not None:
        await main_observation.visual_prime
    video = page.video
    if video is None:  # pragma: no cover - record_video_dir makes this invariant true
        await context.close()
        raise RenderError("Playwright nie udostępnił nagrania głównego okna")

    # Chromium's screencast may not emit a first frame for a pristine about:blank
    # page.  A scenario can narrate for several seconds before its first navigate;
    # anchoring at the Page event would then put that narration on a timeline the
    # WebM never encoded.  Paint a neutral document, force one captured frame, and
    # only then establish the shared narration/window clock.  The tiny warm-up is
    # bounded pre-roll; it avoids losing an arbitrarily long opening narration.
    # With chrome enabled the neutral document IS the shell (bar + empty iframe),
    # so the recording opens on the browser chrome rather than a bare white page.
    # Auto-intro (`cfg.intro.enabled`) replaces this neutral document with a
    # title card instead — render-only, so `intro.enabled=False` keeps today's
    # bootstrap byte-identical.
    site_frame: Frame | None = None
    if chrome is not None:
        site_frame = await chrome.install_shell(page)
    elif not cfg.intro.enabled:
        await page.set_content("<style>html,body{margin:0;background:white}</style>")
    if cfg.intro.enabled:
        active_card = {
            "title": cfg.title,
            "subtitle": cfg.intro.subtitle,
            "notes": cfg.intro.notes,
        }
        await slide.show(page, active_card)
        await overlay.hide(page)
        await _chrome_hide(page)
        card_active = True
    await _ensure_visuals(page, overlay, chrome)
    await page.screenshot()
    await page.wait_for_timeout(100)
    anchor = time.monotonic()

    sfx_events: list[tuple[str, float]] = []

    def sfx_sink(kind: str) -> None:
        sfx_events.append((kind, time.monotonic()))

    placed_by_language: dict[str, list[Placed]] = {tts.lang: [] for tts in audio_configs}
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
            # Card-aware visual prep, ahead of the narration block: a `slide`
            # step paints (replacing any prior card); a `say` step keeps a live
            # card up while it narrates; any other step dismisses the card
            # first (asserting it survived, fail-loud) before its normal
            # `_ensure_visuals`. With no card ever painted this is exactly
            # today's unconditional `_ensure_visuals` call (back-compat).
            if kind == "slide":
                assert step.slide is not None  # guaranteed by command_kind()
                if card_active:
                    # Fail loud before repainting: a slide following a say whose
                    # card was destroyed mid-narration must NOT silently swap in a
                    # fresh card over the wrong page (mirrors the generic dismiss
                    # branch's token assert below).
                    await _assert_card_alive(active_page)
                    await slide.hide(active_page)
                    await overlay.show(active_page)
                    await _chrome_show(active_page)
                active_card = {
                    "title": step.slide.title,
                    "subtitle": step.slide.subtitle,
                    "notes": step.slide.notes,
                }
                await slide.show(active_page, active_card)
                await overlay.hide(active_page)
                await _chrome_hide(active_page)
                card_active = True
            elif kind == "say" and card_active:
                await _ensure_card(active_page)
            elif card_active:
                await _assert_card_alive(active_page)
                await slide.hide(active_page)
                await overlay.show(active_page)
                await _chrome_show(active_page)
                card_active = False
                active_card = None
                await _ensure_visuals(
                    active_page,
                    overlay,
                    chrome,
                    expect_chrome=_expect_chrome(chrome, bare_popups),
                )
            else:
                await _ensure_visuals(
                    active_page,
                    overlay,
                    chrome,
                    expect_chrome=_expect_chrome(chrome, bare_popups),
                )

            step_segments: list[Segment] = []
            narration_offset = time.monotonic() - anchor
            for tts in audio_configs:
                seg = segments[tts.lang].get(index)
                if seg is not None:
                    placed_by_language[tts.lang].append(
                        Placed(segment=seg, offset=narration_offset)
                    )
                    step_segments.append(seg)
            if step_segments:
                # One picture timeline: the action waits for the longest language,
                # while shorter tracks naturally contain silence before the action.
                await _wait_for_step_narration(step_segments)

            _sync_popup_close(popup, observed_pages, anchor)
            if popup is not None and popup.page.is_closed() and not popup.close_handled:
                raise RenderError("popup zamknął się asynchronicznie podczas narracji")
            active_page = _active_page(page, popup)
            if _unexpected_pages(observed_pages, page, popup):
                raise RenderError(f"krok {index}: nieoczekiwany popup — uruchom `compile --force`")
            await active_page.bring_to_front()
            # Card-aware post-narration re-assert: a navigation that destroyed the
            # card DURING the narration wait (a say/slide over a live card) must
            # fail loud here — this is the checkpoint that catches a mid-wait
            # destruction even when the say is the LAST step (the loop still fully
            # processes that step before exiting). When no card is active this is
            # exactly today's unconditional `_ensure_visuals` (back-compat).
            if card_active:
                await _ensure_card(active_page)
            else:
                await _ensure_visuals(
                    active_page,
                    overlay,
                    chrome,
                    expect_chrome=_expect_chrome(chrome, bare_popups),
                )
            cached = compiled.actions[index]
            if cached is not None and cached.opens_popup and popup is not None:
                raise RenderError("v1 obsługuje co najwyżej jeden popup w całej sesji")
            # Main window drives the site iframe (a Frame); popups drive the page.
            on_shell = active_page is page and site_frame is not None
            recorder = Recorder(
                active_page,
                overlay,
                settle_ms=cfg.cursor.settle,
                frame=site_frame if on_shell else None,
                type_delay_ms=(cfg.typing.speed if cfg.typing.animate else None),
                on_sfx=(sfx_sink if cfg.sound.enabled else None),
            )
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
                    _ensure_card,
                    expect_chrome=_expect_chrome(chrome, bare_popups),
                )
                if opened is not None:
                    popup = opened
                    popup.page.set_default_timeout(timeout * 1000)
                    prepared = await _prepare_popup(
                        popup.page,
                        overlay,
                        chrome,
                        expect_chrome=_expect_chrome(chrome, bare_popups),
                    )
                    _sync_popup_close(popup, observed_pages, anchor)
                    if not prepared:
                        raise RenderError("popup zamknął się podczas otwierania")
                if page.is_closed():
                    raise RenderError("główne okno zostało zamknięte podczas render")
                _sync_popup_close(popup, observed_pages, anchor)
                if popup is not None and popup.page.is_closed():
                    if not popup.close_handled:
                        if opened is not None or kind in {"say", "navigate", "wait", "slide"}:
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
                safe_message = redact_exception(exc, sensitive_values)
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
                if pause_on_error:
                    debug_page = _active_page(page, popup)
                    await pause_for_inspection(
                        debug_page,
                        "render",
                        index,
                        kind,
                        exc,
                        sensitive_values,
                    )
                raise RenderError(f"{type(exc).__name__}: {safe_message}") from None
            bar.update(1)
        # Force a bounded final frame after narration/action completion. Without
        # this post-roll, a static last page can leave the VFR recording a fraction
        # shorter than the audio timeline and make the final syllable trimmable.
        await asyncio.sleep(_VIDEO_POSTROLL_SECONDS)
        postroll_page = _active_page(page, popup)
        await postroll_page.screenshot()
        _sync_popup_close(popup, observed_pages, anchor)
        if page.is_closed():
            raise RenderError("główne okno zostało zamknięte na końcu scenariusza")
        if _unexpected_pages(observed_pages, page, popup):
            raise RenderError("nieoczekiwany popup na końcu scenariusza")
        if popup is not None and popup.page.is_closed() and not popup.close_handled:
            raise RenderError("popup zamknął się asynchronicznie na końcu scenariusza")
    finally:
        bar.close()
        _sync_popup_close(popup, observed_pages, anchor)
        if popup is not None and popup.closed_at is None:
            popup_open_at_end = True
            popup.closed_at = max(popup.opened_at, time.monotonic() - anchor)
        prime_tasks = [
            observation.visual_prime
            for observation in observed_pages.values()
            if observation.visual_prime is not None
        ]
        for task in prime_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*prime_tasks, return_exceptions=True)
        await context.close()

    sfx_offsets: list[tuple[str, float]] = []
    if cfg.sound.enabled:
        for kind, t in sfx_events:
            if kind == "click" and not cfg.sound.click:
                continue
            if kind == "key" and not cfg.sound.keys:
                continue
            off = t - anchor
            if off < 0:
                raise RenderError(f"ujemny offset SFX ({off}) — błąd zegara renderu")
            sfx_offsets.append((kind, off))

    main_webm = Path(await video.path())
    if popup is None:
        total = probe_duration(main_webm)
        await _assemble_audio_tracks(
            main_webm,
            audio_configs,
            placed_by_language,
            total,
            work,
            out_mp4,
            sound=cfg.sound,
            sfx_offsets=sfx_offsets,
        )
        return

    popup_webm = Path(await popup.video.path())
    closed_at = probe_duration(main_webm) if popup_open_at_end else popup.closed_at
    assert closed_at is not None
    composite = work / f"{out_mp4.stem}.composite.mp4"
    compose_popup_video(
        main_webm,
        popup_webm,
        composite,
        popup.opened_at,
        closed_at,
        visual_ready_delay=popup.visual_ready_delay,
        transition=cfg.popup.effective_transition,
        slide_ms=cfg.popup.slide_ms,
        scale=cfg.popup.scale,
        corner_radius=cfg.popup.corner_radius,
        shadow=cfg.popup.shadow,
        backdrop_dim=cfg.popup.backdrop_dim,
        backdrop_blur=cfg.popup.backdrop_blur,
        open_ms=cfg.popup.open_ms,
        close_ms=cfg.popup.close_ms,
        hold_open_at_end=popup_open_at_end,
    )
    total = probe_duration(composite)
    await _assemble_audio_tracks(
        composite,
        audio_configs,
        placed_by_language,
        total,
        work,
        out_mp4,
        preencoded=True,
        sound=cfg.sound,
        sfx_offsets=sfx_offsets,
    )


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
    ensure_card: Callable[[Page], Awaitable[None]],
    *,
    expect_chrome: bool | None = None,
) -> _PopupSession | None:
    if expect_chrome is None:
        expect_chrome = chrome is not None
    pages_before_prepare = set(observed_pages)
    # Both visual layers can be removed by an SPA without a navigation.  Check
    # them before every recorded step, including narration-only and timed waits.
    # ``expect_chrome`` is False when ``page`` is a bare (floating) popup.
    await _ensure_visuals(page, overlay, chrome, expect_chrome=expect_chrome)

    # Locators/navigation/reuse run against the recorder's frame: the site iframe
    # for the main window (a Frame distinct from the shell page), the page itself
    # for popups / chrome-disabled renders.
    action_frame = getattr(recorder, "frame", recorder.page)
    on_shell = action_frame is not recorder.page

    if kind == "say":
        return None
    if kind == "slide":
        assert step.slide is not None  # guaranteed by command_kind()
        if _narration(step) is not None:
            # The loop already waited out the narration before calling us (one
            # picture timeline); re-assert the card and force a captured frame.
            await ensure_card(page)
            await page.screenshot()
            return None
        # No `say` on this slide: hold the card ourselves, SPA-safe — re-assert
        # on a short cadence rather than a single blind sleep, so a same-
        # document rewrite mid-hold is repaired (and a real navigation still
        # fails loud via `ensure_card`'s token check).
        deadline = time.monotonic() + step.slide.hold
        while True:
            await ensure_card(page)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.1, remaining))
    if kind == "navigate":
        source_url = step.navigate_url()
        assert source_url is not None  # guaranteed by command_kind()
        url = _resolve_url(scenario, source_url)
        chrome_cfg = scenario.config.chrome
        show_url = chrome is not None and chrome_cfg.show_url
        mode = navigate_pill_mode(chrome_cfg, step.navigate_type_override())

        if on_shell:
            # Main window: the pill lives in the shell. The choreography/typed
            # animation runs before goto; the truthful site URL (after redirects)
            # is reflected once the iframe has loaded.
            if show_url and mode in ("choreograph", "type"):
                await chrome.type_url(
                    page,
                    overlay,
                    url,
                    seed=f"{url}:{index}",
                    choreograph=(mode == "choreograph"),
                )
            await recorder.navigate(url)
            if show_url:
                await chrome.set_url_shell(page, action_frame.url)
        else:
            # Popup / chrome-disabled: legacy in-DOM pill on the page itself. An
            # instant update happens after goto so redirects are reflected; the
            # animated variant is typed before goto. A bare (floating) popup has
            # no legacy bar/API (chrome.js bailed on barePopups), so gate the pill
            # on ``expect_chrome`` — otherwise chrome.set_url would evaluate an
            # undefined ``window.__guidebot_chrome`` and throw an opaque TypeError.
            if show_url and expect_chrome and mode != "instant":
                await chrome.set_url(page, url, animate=True)
            await recorder.navigate(url)
            if show_url and expect_chrome and mode == "instant":
                await chrome.set_url(page, page.url, animate=False)
        await _ensure_visuals(page, overlay, chrome, expect_chrome=expect_chrome)
        return None
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return None

    if cached is None:
        raise RenderError(f"krok {index}: brak cachedAction — uruchom `compile`")
    if cached.action != "waitFor" and not await reuse_is_valid(action_frame, cached):
        raise RenderError(f"krok {index}: niezgodna tożsamość — uruchom `compile --force`")
    if cached.opens_popup and cached.action != "click":
        raise RenderError(f"krok {index}: tylko click może otworzyć popup")

    opened: _PopupSession | None = None
    if cached.action == "click":
        click_started_at: float | None = None

        def mark_click_started() -> None:
            nonlocal click_started_at
            if any(candidate not in pages_before_prepare for candidate in observed_pages):
                raise RenderError(f"krok {index}: popup otworzył się przed akcją click")
            click_started_at = time.monotonic()

        await recorder.click(cached.target, before_click=mark_click_started)
        if cached.opens_popup:
            if click_started_at is None:  # pragma: no cover - Recorder invariant
                raise RenderError("wewnętrzny błąd obserwacji akcji click")
            popup_pages = await _wait_for_render_popup(
                observed_pages,
                pages_before_prepare,
                click_started_at,
            )
            if not popup_pages:
                raise RenderError(
                    f"krok {index}: oczekiwany popup nie otworzył się — uruchom `compile --force`"
                )
            if len(popup_pages) != 1:
                raise RenderError("v1 obsługuje dokładnie jeden popup w sesji")
            popup_page = popup_pages[0]
            if await popup_page.opener() is not page:
                raise RenderError("nowa strona nie jest popupem aktywnego okna")

            observation = observed_pages.get(popup_page)
            if observation is None:  # defensive fallback; context event is the primary path
                observation = _PageObservation(
                    opened_at=time.monotonic(),
                    video=popup_page.video,
                    closed_at=time.monotonic() if popup_page.is_closed() else None,
                )
                observed_pages[popup_page] = observation
            visual_ready_at = (
                await observation.visual_prime if observation.visual_prime is not None else None
            )
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
                visual_ready_delay=(
                    max(0.0, visual_ready_at - observation.opened_at)
                    if visual_ready_at is not None
                    else 0.0
                ),
                closed_at=closed_at,
            )
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
        try:
            await recorder.apply_readiness(cached.expect)
        except PlaywrightError:
            if not page.is_closed():
                raise
    return opened


async def _prepare_popup(
    page: Page,
    overlay: Overlay,
    chrome: Chrome | None,
    *,
    expect_chrome: bool | None = None,
) -> bool:
    """Prepare a new page; translate close races into lifecycle state.

    ``expect_chrome`` defaults to ``chrome is not None``; pass ``False`` for a
    bare (floating) popup so the chrome bar is not mounted on it (the cursor is
    still ensured).
    """

    if expect_chrome is None:
        expect_chrome = chrome is not None
    if page.is_closed():
        return False
    try:
        await page.bring_to_front()
        await page.wait_for_load_state()
        await overlay.ensure(page)
        if chrome is not None and expect_chrome:
            await chrome.ensure(page)
    except PlaywrightError:
        if page.is_closed():
            return False
        raise
    return not page.is_closed()
