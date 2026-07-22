"""``run_render``: the whole render pass, top to bottom. Phase 3 decomposes it.

Deliberately one opaque module, and deliberately **still over the 600-line limit**
— phase 1 split the file, it did not decompose this function. ``run_render``
carries cyclomatic complexity 97 and three interleaved lifetimes (the frozen plan,
the recording clock, what is currently on screen); turning those into three state
objects is phase 3's job. Moving it verbatim is what keeps this diff reviewable and
keeps the two orderings below provably untouched.

**Two orderings in here are load-bearing.**

* ``cursor.js`` / ``slide.js`` / ``desktop.js`` MUST be registered before
  ``chrome.js``. Each decides its role by reading the real ``window.top``, and
  ``chrome.js`` is what shadows ``top`` (frame-bust neutralization); a layer
  registered after it would misidentify as the top window and mount inside the
  framed site. The long comment beside the ``install_context`` calls is the
  contract, and ``test_render.py`` asserts the registration order with
  ``DesktopOverlay`` included.
* Popup composition MUST run before time editing. Popups are composed on the
  *recording* axis (their ``opened_at``/``closed_at`` are raw wall clock); time
  editing is what moves narration and SFX onto the *virtual* axis. Swapping them
  yields a film of the right length with the popup in the wrong place, and
  ``test_popup_is_composed_before_time_editing_and_feeds_it`` (phase 0) asserts
  both the call order and that the edit consumes the compositor's output.

Every test seam this function drives is called through a module object —
``narration._pace_narration``, ``timeline_module._apply_timeline_edits``,
``audio._assemble_audio_tracks``, ``_step._render_step``,
``visuals._prepare_main_after_popup_close`` — so a patch on the defining submodule
lands on the globals read here. The seams defined *outside* the package
(``Overlay``, ``SlideOverlay``, ``Recorder``, ``compose_popup_video``,
``probe_frame_count``) are name-imported instead, which makes *this* module their
patch target; ``Recorder`` and ``probe_frame_count`` have a second consumer inside
the package and therefore need two patch lines each.

``timeline`` is imported as ``timeline_module`` because ``timeline`` is already a
local name in ``run_render`` — the same reason ``mux_probe`` is aliased below.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path

from playwright.async_api import Browser, Frame, Page
from tqdm import tqdm

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.desktop import DesktopOverlay
from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import pause_for_inspection, redact_exception
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.recorder.session import ensure_session
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import ResolvedTarget
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError, install_selects
from guidebot_recorder.slide import SlideOverlay
from guidebot_recorder.tts.base import Segment, TtsProvider
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux import FadeSpec, compose_popup_video

# `probe_duration` is a test seam: the mux facade withholds it and the call must
# stay late-bound, so its defining module is imported here instead. Aliased
# because `probe` is already used as a local name elsewhere in this module.
from guidebot_recorder.video.mux import probe as mux_probe
from guidebot_recorder.video.timeline import (
    TimeEdit,
    assert_recording_fps,
    frames_to_seconds,
    probe_frame_count,
)

from . import _step, audio, narration, visuals
from . import timeline as timeline_module
from .errors import RenderError, _OptionalAbsent
from .narration import _stamp_frame
from .pages import _active_page, _expect_chrome
from .plan import _prepare_render
from .popup_crop import _popup_fills_canvas, _resolve_popup_crop, _settle_popup_content_box
from .popup_detect import _POPUP_REQUEST_SCRIPT, _popup_window_opened
from .popup_session import (
    _PageObservation,
    _PopupSession,
    _prepare_popup,
    _sync_popup_close,
    _unexpected_pages,
)
from .reuse import _resolve_pending_target
from .timeline import _build_timeline
from .visuals import _ensure_visuals, _hand_cursor_to_popup, _play_desktop_opener, _prime_visuals

_VIDEO_POSTROLL_SECONDS = 0.1


#: A slide card's on-screen content, as consumed by ``SlideOverlay.show``/``.ensure``.
Card = dict[str, str | None]


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
    hold_frame: bool | None = None,
    hold_frame_settle: float | None = None,
    dump_timeline: bool = False,
    reasoner: Reasoner | None = None,
) -> None:
    plan = await _prepare_render(
        path,
        out_mp4,
        tts_provider,
        cache_dir,
        env=env,
        hold_frame=hold_frame,
        hold_frame_settle=hold_frame_settle,
        verbose=verbose,
    )
    # Read-only aliases onto the frozen plan. The plan is the single source of
    # truth; these exist so the phases below still read as prose rather than as
    # `plan.` repeated three hundred times.
    cfg = plan.cfg
    scenario = plan.scenario
    flat = plan.flat
    compiled = plan.compiled
    audio_configs = plan.audio_configs
    segments = plan.segments
    sensitive_values = plan.sensitive_values
    scenario_hash = plan.scenario_hash
    desktop_payloads = plan.desktop_payloads
    step_message = plan.step_message
    path = plan.path
    out_mp4 = plan.out_mp4

    # --- Render z nagrywaniem wideo (viewport z config — patrz compile) ---
    work = plan.work
    work.mkdir(parents=True, exist_ok=True)
    # The context viewport and video size stay at the configured dimensions so the
    # output MP4 keeps its size and popups are geometrically untouched; the shell
    # shrinks only the site iframe interior (see compile / site_viewport).
    # Both settings are context-level, so a popup also records onto a
    # main-viewport-sized canvas with filler around its real window. That is
    # corrected in post (``compose_popup_video(popup_crop=...)``), never here:
    # shrinking the recording would also shrink the main window's frame.
    #
    # Pre-recording setup: when the target declares ``config.setup`` its login
    # steps were removed, so the recording context must start already logged in.
    # ``ensure_session`` establishes/reuses the prepared session on separate,
    # non-recording contexts *before* this line, so the login can never reach the
    # film (spec: "Target render").
    setup_state = (
        await ensure_session(browser, Path(path), Path(".guidebot/sessions"), env, timeout=timeout)
        if cfg.setup is not None
        else None
    )
    context = await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        record_video_dir=str(work),
        record_video_size={"width": cfg.viewport.width, "height": cfg.viewport.height},
        **({"storage_state": setup_state} if setup_state is not None else {}),
        **({"bypass_csp": True, "service_workers": "block"} if cfg.chrome.enabled else {}),
    )
    # Independent of the role-gating order below (it only wraps ``window.open``),
    # but registered first so it wraps the *native* function on every document.
    await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
    overlay = Overlay(cfg.cursor, cfg.viewport)
    # Role-gating contract: cursor.js, slide.js and desktop.js MUST be registered
    # before chrome.js. Inside the site iframe, each of them decides its role by
    # reading the real ``window.top`` (cursor.js to skip mounting a duplicate
    # cursor, slide.js's ``isTop`` guard to skip installing
    # ``window.__guidebot_slide``, desktop.js likewise); chrome.js is what
    # shadows ``top`` (frame-bust neutralization). If any of these init scripts
    # ran after chrome.js, it would read the shadowed ``top``, misidentify as the
    # top window, and mount inside the frame.
    #
    # selects.js reads ``top`` too but is deliberately NOT part of that contract:
    # its only test is ``isTop && origin === SHELL_ORIGIN``, and chrome.js
    # shadows ``top`` solely inside framed documents, whose origin is never the
    # shell's — so the shim reaches the same verdict on either side of chrome.js.
    # It is registered here anyway, next to the overlays it sits beside; nothing
    # downstream may rely on that position. See the role-gating comment at the
    # top of ``selects/selects.js``.
    await overlay.install_context(context)
    slide = SlideOverlay()
    await slide.install_context(context)
    # Same role-gating rationale as slide.js (isTop guard): must be registered
    # before chrome.js so it reads the real ``window.top`` and never mounts the
    # desktop inside the framed site.
    desktop = DesktopOverlay(config={"background": cfg.desktop.color})
    await desktop.install_context(context)
    # The DOM select shim — one of the three contexts that drive pages (spec §1),
    # and the reason the recording shows an option list at all. ``None`` under
    # ``selects.mode: native``, which keeps the page's own control.
    selects = await install_selects(context, cfg)
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
    # `card` is the slide card that currently owns the screen (painted either by a
    # `slide` step or the auto-intro below), or None when the page itself is on
    # screen. One variable, not a `(bool, payload)` pair: the two halves were
    # written together at every site and the code asserted they agreed, so the
    # only thing a pair could express was a desync. When no card is ever painted
    # (no `slide` steps, `intro.enabled=False`) it stays None for the whole render
    # and every helper below is a pure pass-through to today's `_ensure_visuals` —
    # i.e. byte-identical back-compat.
    card: Card | None = None

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
        assert card is not None  # only ever called on the card-active path
        await slide.ensure(pg, card)
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
        card = {
            "title": cfg.title,
            "subtitle": cfg.intro.subtitle,
            "notes": cfg.intro.notes,
        }
        await slide.show(page, card)
        await overlay.hide(page)
        await _chrome_hide(page)
    await _ensure_visuals(page, overlay, chrome)
    await page.screenshot()
    await page.wait_for_timeout(100)
    anchor = time.monotonic()

    # Audio placements are collected as recording-axis FRAMES, not seconds: the
    # grid is what `Timeline` reasons on, and quantising once at the moment of
    # observation is what lets `_stamp_frame` keep them monotonic against the
    # freezes. Seconds reappear only at the very end, when the audio bed is built.
    sfx_events: list[tuple[str, int]] = []
    # Freezes recorded while rendering, on the *recording* axis. Applied to the
    # video — and used to remap every audio offset — once the loop is done.
    time_edits: list[TimeEdit] = []
    # Frame of the most recent freeze, or -1 before any. Read by `_stamp_frame`
    # so nothing is ever stamped inside a hold that was already recorded.
    last_freeze_frame = -1

    def sfx_sink(kind: str) -> None:
        sfx_events.append((kind, _stamp_frame(anchor, not_before=last_freeze_frame + 1)))

    placed_by_language: dict[str, list[tuple[Segment, int]]] = {
        tts.lang: [] for tts in audio_configs
    }
    popup: _PopupSession | None = None
    popup_open_at_end = False

    #: branch whose gate turned out to be absent — every step of it is skipped
    skipped_branch: int | None = None

    bar = tqdm(total=len(flat), desc="render", unit="krok", disable=not verbose)
    try:
        for index, entry in enumerate(flat):
            step = entry.step
            if skipped_branch is not None and entry.branch == skipped_branch:
                # The gate never showed: the branch's children never run, and their
                # narration is removed from the timeline rather than left as silence
                # (segments are placed per index, so never placing them removes them).
                bar.update(1)
                continue
            skipped_branch = None
            _sync_popup_close(popup, observed_pages, anchor)
            if popup is not None and popup.page.is_closed() and not popup.close_handled:
                raise RenderError("popup zamknął się poza obsługiwaną akcją scenariusza")
            if _unexpected_pages(observed_pages, page, popup):
                raise RenderError(
                    step_message(entry, index, "nieoczekiwany popup — uruchom `compile --force`")
                )
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(flat)}] {kind}")

            active_page = _active_page(page, popup)
            await active_page.bring_to_front()
            # Card-aware visual prep, ahead of the narration block: a `slide`
            # step paints (replacing any prior card); a `say` step keeps a live
            # card up while it narrates; any other step dismisses the card
            # first (asserting it survived, fail-loud) before its normal
            # `_ensure_visuals`. With no card ever painted this is exactly
            # today's unconditional `_ensure_visuals` call (back-compat).
            if kind == "desktop":
                assert step.desktop is not None  # guaranteed by command_kind()
                if card is not None:
                    await _assert_card_alive(active_page)
                    await slide.hide(active_page)
                    card = None

                async def _reveal_shell(pg: Page = active_page) -> None:
                    await _chrome_show(pg)

                await _play_desktop_opener(
                    desktop,
                    overlay,
                    active_page,
                    desktop_payloads[index],
                    hold=step.desktop.hold,
                    settle_ms=cfg.cursor.settle,
                    reveal=_reveal_shell,
                    on_click=(sfx_sink if cfg.sound.enabled else None),
                )
                # The opener ends on the revealed chrome shell — normal visible
                # state, so from here it is exactly the no-card path (`card`
                # stays None).
            elif kind == "slide":
                assert step.slide is not None  # guaranteed by command_kind()
                if card is not None:
                    # Fail loud before repainting: a slide following a say whose
                    # card was destroyed mid-narration must NOT silently swap in a
                    # fresh card over the wrong page (mirrors the generic dismiss
                    # branch's token assert below).
                    await _assert_card_alive(active_page)
                    await slide.hide(active_page)
                    await overlay.show(active_page)
                    await _chrome_show(active_page)
                card = {
                    "title": step.slide.title,
                    "subtitle": step.slide.subtitle,
                    "notes": step.slide.notes,
                }
                await slide.show(active_page, card)
                await overlay.hide(active_page)
                await _chrome_hide(active_page)
            elif kind == "say" and card is not None:
                await _ensure_card(active_page)
            elif card is not None:
                await _assert_card_alive(active_page)
                await slide.hide(active_page)
                await overlay.show(active_page)
                await _chrome_show(active_page)
                card = None
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

            # --- absence probe / in-place resolution, ahead of the narration ----
            # An optional step that turns out to be absent must not narrate first
            # and only then do nothing, so everything decidable before the action —
            # a stale frozen target, an unresolvable pending entry — is decided
            # here. A cached gate is the exception: its `waitFor` IS the action, so
            # it stays in `_step._render_step` (a synthetic gate step never
            # narrates).
            #
            # `optional` marks the only two places absence is tolerated: a branch
            # gate and an `optional: true` step. A *child* of an entered branch is
            # not optional — the branch demonstrably happened, so anything failing
            # inside it is a real regression (§5 of the design). Its pending entry
            # is still resolved here; only the verdict on absence differs.
            optional = entry.is_gate or step.optional
            resolved: ResolvedTarget | None = None
            cached = compiled.actions[index]
            # The site iframe for the main window, the page itself for popups /
            # chrome-disabled renders — never the shell document, which the shim
            # deliberately skips.
            probe_root: Page | Frame = (
                site_frame if active_page is page and site_frame is not None else active_page
            )
            if selects is not None and step.requires_target():
                # Readiness barrier, the mirror of compile's: both the in-place
                # resolution below and the frozen-target check inside
                # ``_render_step`` must see the shimmed DOM, or render would drive
                # a page compile never resolved against. Any navigation that led
                # here has settled — it was an earlier step.
                try:
                    await selects.wait_ready(probe_root)
                except SelectsNotReadyError as exc:
                    # The barrier sits outside every per-step ``except`` in this
                    # loop, so without this the one failure that stops a render
                    # before its step even begins would be the only one to reach
                    # the author with no file, no line and no YAML fragment.
                    raise RenderError(step_message(entry, index, str(exc))) from exc
            if step.requires_target() and (optional or isinstance(cached, PendingAction)):
                try:
                    if isinstance(cached, PendingAction):
                        if reasoner is None:
                            raise _OptionalAbsent(
                                "brak dostępnego reasonera, a krok nie został skompilowany "
                                "(pending) — zainstaluj `codex`, aby rozwiązać go na miejscu"
                            )
                        resolved = await _resolve_pending_target(probe_root, step, kind, reasoner)
                    elif isinstance(cached, CachedAction) and cached.action != "waitFor":
                        if not await reuse_is_valid(probe_root, cached):
                            raise _OptionalAbsent("zamrożony namiar nie pasuje do strony")
                except _OptionalAbsent as absent:
                    if not optional:
                        raise RenderError(step_message(entry, index, str(absent))) from None
                    plan.note_skip(entry, index, str(absent), gate=entry.is_gate)
                    if entry.is_gate:
                        skipped_branch = entry.branch
                    bar.update(1)
                    continue
                except Exception as exc:
                    # Everything the resolver rejects for a reason other than
                    # absence — `multiple_actions` above all — is an authoring bug
                    # and fails the render, exactly as in the action loop below.
                    safe_message = redact_exception(exc, sensitive_values)
                    if verbose:
                        tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
                    if pause_on_error:
                        await pause_for_inspection(
                            _active_page(page, popup),
                            "render",
                            index,
                            kind,
                            exc,
                            sensitive_values,
                            total=len(flat),
                            location=entry.location,
                            source=scenario.source,
                        )
                    raise RenderError(f"{type(exc).__name__}: {safe_message}") from None

            step_segments: list[Segment] = []
            # Recording-axis frame: the mapping onto the finished film needs the
            # complete edit list, which does not exist until the loop ends.
            narration_frame = _stamp_frame(anchor, not_before=last_freeze_frame + 1)
            for tts in audio_configs:
                seg = segments[tts.lang].get(index)
                if seg is not None:
                    placed_by_language[tts.lang].append((seg, narration_frame))
                    step_segments.append(seg)
            if step_segments:
                # One picture timeline: the action waits for the longest language,
                # while shorter tracks naturally contain silence before the action.
                emitted = await narration._pace_narration(
                    step_segments,
                    anchor=anchor,
                    hold_frame=cfg.hold_frame_for_narration,
                    settle=cfg.hold_frame_settle,
                    edits=time_edits,
                    not_before=narration_frame,
                )
                if emitted is not None:
                    last_freeze_frame = emitted

            _sync_popup_close(popup, observed_pages, anchor)
            if popup is not None and popup.page.is_closed() and not popup.close_handled:
                raise RenderError("popup zamknął się asynchronicznie podczas narracji")
            active_page = _active_page(page, popup)
            if _unexpected_pages(observed_pages, page, popup):
                raise RenderError(
                    step_message(entry, index, "nieoczekiwany popup — uruchom `compile --force`")
                )
            await active_page.bring_to_front()
            # Card-aware post-narration re-assert: a navigation that destroyed the
            # card DURING the narration wait (a say/slide over a live card) must
            # fail loud here — this is the checkpoint that catches a mid-wait
            # destruction even when the say is the LAST step (the loop still fully
            # processes that step before exiting). When no card is active this is
            # exactly today's unconditional `_ensure_visuals` (back-compat).
            if card is not None:
                await _ensure_card(active_page)
            else:
                await _ensure_visuals(
                    active_page,
                    overlay,
                    chrome,
                    expect_chrome=(
                        popup.wants_bar
                        if popup is not None and active_page is popup.page
                        else _expect_chrome(chrome, bare_popups)
                    ),
                )
            if isinstance(cached, CachedAction) and cached.opens_popup and popup is not None:
                raise RenderError("v1 obsługuje co najwyżej jeden popup w całej sesji")
            if kind == "closeWindow" and popup is None:
                raise RenderError(step_message(entry, index, "closeWindow bez otwartego okna"))
            # Main window drives the site iframe (a Frame); popups drive the page.
            on_shell = active_page is page and site_frame is not None
            recorder = Recorder(
                active_page,
                overlay,
                settle_ms=cfg.cursor.settle,
                frame=site_frame if on_shell else None,
                type_delay_ms=(cfg.typing.speed if cfg.typing.animate else None),
                type_jitter_ms=cfg.typing.jitter_ms,
                type_max_delay_factor=cfg.typing.max_delay_factor,
                on_sfx=(sfx_sink if cfg.sound.enabled else None),
                # How long the unfurled option list is held before the cursor
                # sets off towards the chosen row. Render is the only phase that
                # animates a `select:` step, so this is the one place the
                # configured value can take effect at all.
                open_hold_ms=cfg.selects.open_hold_ms,
            )
            try:
                opened = await _step._render_step(
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
                    entry=entry,
                    total=len(flat),
                    sensitive=sensitive_values,
                    expect_chrome=(
                        popup.wants_bar
                        if popup is not None and active_page is popup.page
                        else _expect_chrome(chrome, bare_popups)
                    ),
                    resolved=resolved,
                    optional=optional,
                    scenario_hash=scenario_hash,
                    on_resolved=plan.persist_resolved,
                )
                if opened is not None:
                    popup = opened
                    popup.page.set_default_timeout(timeout * 1000)
                    popup.is_blank_tab = not await _popup_window_opened(page)
                    popup.wants_bar = chrome is not None and popup.is_blank_tab
                    prepared = await _prepare_popup(
                        popup.page,
                        overlay,
                        chrome,
                        expect_chrome=_expect_chrome(chrome, bare_popups) or popup.wants_bar,
                        mount_bar=popup.wants_bar,
                    )
                    _sync_popup_close(popup, observed_pages, anchor)
                    if not prepared:
                        raise RenderError("popup zamknął się podczas otwierania")
                    # The popup now owns the cursor (it mounted its own); stop
                    # painting a second one in the main window behind it.
                    await _hand_cursor_to_popup(page, popup, overlay)
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
                        await visuals._prepare_main_after_popup_close(
                            page,
                            overlay,
                            chrome,
                            cfg.cursor.settle,
                            restore_cursor_to=popup.main_cursor_pos,
                        )
                if _unexpected_pages(observed_pages, page, popup):
                    raise RenderError(
                        step_message(
                            entry, index, "nieoczekiwany popup — uruchom `compile --force`"
                        )
                    )
            except _OptionalAbsent as absent:
                # Only a cached gate reaches here (its `waitFor` timed out); every
                # other absence signal was already settled by the probe above.
                plan.note_skip(entry, index, str(absent), gate=entry.is_gate)
                if entry.is_gate:
                    skipped_branch = entry.branch
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
                        total=len(flat),
                        location=entry.location,
                        source=scenario.source,
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
        if popup is not None:
            # Last moment the popup's DOM can still answer: the context (and with
            # it every page) is closed a few lines below. The probe was started
            # when the popup opened, so this normally settles instantly.
            await _settle_popup_content_box(popup)
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

    sfx_frames: list[tuple[str, int]] = []
    if cfg.sound.enabled:
        for kind, frame in sfx_events:
            if kind == "click" and not cfg.sound.click:
                continue
            if kind == "key" and not cfg.sound.keys:
                continue
            if frame < 0:
                raise RenderError(f"ujemna klatka SFX ({frame}) — błąd zegara renderu")
            sfx_frames.append((kind, frame))

    main_webm = Path(await video.path())
    if popup is None:
        source_video = main_webm
        preencoded = False
    else:
        popup_webm = Path(await popup.video.path())
        # The popup recorded onto the main window's canvas; crop it back to its
        # real window so float frames that and not a viewport-sized rectangle of
        # filler. Three levels, best evidence first; all declining -> today's
        # full canvas.
        popup_crop, _crop_level = _resolve_popup_crop(
            window_size=popup.window_size,
            content_box=popup.content_box,
            popup_video=popup_webm,
            verbose=verbose,
            viewport=popup.viewport,
            canvas=(cfg.viewport.width, cfg.viewport.height),
        )
        # A real browser TAB that fills the canvas is not a floating popup:
        # `slide` is the full-frame presentation by design and ignores
        # `popup_crop`, while `float` would inset a whole viewport and read as a
        # shrunken clone of the page. Gated on `is_blank_tab`, not on the crop
        # alone — a featureless `window.open` painting a full-bleed background
        # also declines every crop level, yet is a genuine floating window that
        # must keep `float`. Only `float` is overridden: an author who asked for
        # `cut` gets the hard cut they asked for.
        transition = cfg.popup.effective_transition
        if (
            transition == "float"
            and popup.is_blank_tab
            and _popup_fills_canvas(popup_crop, cfg.viewport)
        ):
            transition = "slide"
            if verbose:
                tqdm.write("popup wypełnia kadr — wymuszam przejście `slide` zamiast `float`")
        closed_at = mux_probe.probe_duration(main_webm) if popup_open_at_end else popup.closed_at
        assert closed_at is not None
        composite = work / f"{out_mp4.stem}.composite.mp4"
        compose_popup_video(
            main_webm,
            popup_webm,
            composite,
            popup.opened_at,
            closed_at,
            visual_ready_delay=popup.visual_ready_delay,
            transition=transition,
            slide_ms=cfg.popup.slide_ms,
            scale=cfg.popup.scale,
            corner_radius=cfg.popup.corner_radius,
            shadow=cfg.popup.shadow,
            backdrop_dim=cfg.popup.backdrop_dim,
            backdrop_blur=cfg.popup.backdrop_blur,
            open_ms=cfg.popup.open_ms,
            close_ms=cfg.popup.close_ms,
            hold_open_at_end=popup_open_at_end,
            popup_crop=popup_crop,
        )
        source_video = composite
        preencoded = True

    # Time editing runs AFTER popup composition: popups are composed on the
    # recording axis (their opened_at/closed_at are raw wall clock) and must stay
    # there. Only what is consumed downstream — narration and SFX — moves onto
    # the virtual axis.
    timeline = _build_timeline(time_edits, source_frames=probe_frame_count(source_video))
    if dump_timeline:
        out_mp4.with_suffix(".timeline.json").write_text(timeline.to_json(), encoding="utf-8")
    if not timeline.is_empty:
        assert_recording_fps(source_video)
        edited = work / f"{out_mp4.stem}.timeline.mp4"
        timeline_module._apply_timeline_edits(source_video, timeline, edited)
        source_video = edited
        preencoded = True

    # Taken from the model rather than probed, which is what makes the audio and
    # video axes agree by construction.
    total = timeline.virtual_duration
    # The one place frames become seconds: mapped on the grid, then converted.
    placed_tracks = {
        lang: [
            Placed(segment=seg, offset=frames_to_seconds(timeline.to_virtual(frame)))
            for seg, frame in placed
        ]
        for lang, placed in placed_by_language.items()
    }
    sfx_offsets = [
        (kind, frames_to_seconds(timeline.to_virtual(frame))) for kind, frame in sfx_frames
    ]

    await audio._assemble_audio_tracks(
        source_video,
        audio_configs,
        placed_tracks,
        total,
        work,
        out_mp4,
        preencoded=preencoded,
        sound=cfg.sound,
        sfx_offsets=sfx_offsets,
        fade=(
            FadeSpec(
                fade_in=cfg.fade.fade_in,
                fade_out=cfg.fade.fade_out,
                color=cfg.fade.color,
                audio=cfg.fade.audio,
            )
            if cfg.fade.enabled
            else None
        ),
    )
