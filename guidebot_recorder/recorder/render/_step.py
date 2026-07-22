"""``_render_step``: replay exactly one flat step. Phase 3 decomposes it.

Deliberately one opaque module holding one function. ``_render_step`` is two
dispatches on two different keys — the scenario ``kind`` and the sidecar
``cached.action``, in a many-to-many relation — and phase 1 moved it verbatim
rather than guessing at a decomposition. Its cyclomatic complexity (49) is
unchanged and is phase 3's subject; the leading underscore in the module name says
the same thing.

``_render_step`` is a test seam: defined here, called through this module object
from :mod:`~guidebot_recorder.recorder.render._run`.

``Overlay`` and ``Recorder`` are annotated through their module objects rather than
name-imported. Both are seams patched elsewhere — ``Recorder`` on
:mod:`~guidebot_recorder.recorder.render.visuals` and ``_run``, ``Overlay`` on
``_run`` — and a bare name-import here would be an import-time copy no patch
reaches.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Page
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
from guidebot_recorder.overlay import overlay as overlay_module
from guidebot_recorder.recorder import recorder as recorder_module
from guidebot_recorder.recorder.recorder import SelectDriveError
from guidebot_recorder.resolver.resolution import ResolvedTarget, heuristic_expect
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError

from .errors import RenderError, _OptionalAbsent
from .narration import _narration
from .pages import navigate_pill_mode
from .popup_crop import _page_viewport, _start_popup_content_box
from .popup_detect import _popup_window_request
from .popup_session import _PageObservation, _PopupSession, _wait_for_render_popup
from .reuse import _freeze_resolved, _resolve_url
from .visuals import _ensure_visuals


async def _render_step(
    page: Page,
    recorder: recorder_module.Recorder,
    overlay: overlay_module.Overlay,
    chrome: Chrome | None,
    scenario: Scenario,
    step: Step,
    kind: str,
    index: int,
    cached: CompiledAction | None,
    anchor: float,
    observed_pages: dict[Page, _PageObservation],
    ensure_card: Callable[[Page], Awaitable[None]],
    *,
    entry: FlatStep | None = None,
    total: int = 0,
    sensitive: Iterable[str] = (),
    expect_chrome: bool | None = None,
    resolved: ResolvedTarget | None = None,
    optional: bool = False,
    scenario_hash: str = "",
    on_resolved: Callable[[int, CachedAction], None] | None = None,
) -> _PopupSession | None:
    """Replay one flat step.

    ``resolved`` carries a target the caller resolved in place for a
    :class:`PendingAction`; this call performs it, derives its ``expect`` the way
    ``compile`` does (URL before vs after) and hands the frozen action back through
    ``on_resolved`` so the sidecar stops being pending.

    ``entry`` (plus ``total`` i ``sensitive``) służy wyłącznie diagnostyce:
    komunikaty błędów wskazują `plik:linia` i cytują fragment YAML-a. Wszystkie
    trzy są keyword-only i mają wartości domyślne — pozycje argumentów pozostają
    nietknięte, a bez nich banner degraduje się do samego numeru kroku.
    """

    def step_message(message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=index,
            total=total,
            location=entry.location if entry is not None else None,
            source=scenario.source,
            message=message,
            sensitive=sensitive,
        )

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
    if kind == "desktop":
        # The opener's whole choreography (paint, cursor arc, double-click, window
        # growth) already ran in the card block before narration; nothing is left
        # to do in the action phase. Mirrors the visual-only `slide`/`say` returns.
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
    if kind == "closeWindow":
        # The loop's popup-lifecycle check sees the closed page next and runs
        # `visuals._prepare_main_after_popup_close` with the saved cursor position.
        # Do not
        # duplicate that here: calling the funnel without `restore_cursor_to`
        # leaves the main window's cursor at the popup's centre.
        await page.close()
        return None
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
                    on_sfx=recorder.on_sfx,
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
    if kind == "scroll":
        await recorder.scroll(step.scroll_config())
        return None

    url_before = action_frame.url
    if resolved is not None:
        # Resolved in place a moment ago against this very frame: it is live by
        # construction, and `expect` is only knowable after the action has run.
        cached = _freeze_resolved(step, kind, resolved, "none", scenario_hash)
    elif cached is None:
        raise RenderError(step_message("brak cachedAction — uruchom `compile`"))
    elif isinstance(cached, PendingAction):  # pragma: no cover - prologue rejects these
        raise RenderError(step_message("nierozwiązany wpis oczekujący — uruchom `compile`"))
    elif cached.action != "waitFor" and not await reuse_is_valid(action_frame, cached):
        raise RenderError(step_message("niezgodna tożsamość — uruchom `compile --force`"))
    assert isinstance(cached, CachedAction)
    if cached.opens_popup and cached.action != "click":
        raise RenderError(step_message("tylko click może otworzyć popup"))

    opened: _PopupSession | None = None
    if cached.action == "click":
        click_started_at: float | None = None

        def mark_click_started() -> None:
            nonlocal click_started_at
            if any(candidate not in pages_before_prepare for candidate in observed_pages):
                raise RenderError(step_message("popup otworzył się przed akcją click"))
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
                    step_message("oczekiwany popup nie otworzył się — uruchom `compile --force`")
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
                # Read from the opener while it is still alive.
                window_size=await _popup_window_request(page),
                # Available the instant the popup opens, and authoritative: a
                # popup opened with size features reports that size, a featureless
                # one reports the context viewport it inherited (which is exactly
                # the case levels 2 and 3 exist for).
                viewport=_page_viewport(popup_page),
                # Level 2, started (not awaited) now because by composition time
                # the popup page is closed. Only the *painted* content can be
                # measured: a featureless popup's layout viewport is the
                # context's, so innerWidth/outerWidth/clientWidth all restate the
                # oversized number instead of the real window. Awaiting it here
                # would spend its latency on camera; it is reaped before the
                # context closes instead.
                content_box_probe=_start_popup_content_box(popup_page),
            )
    elif cached.action == "hover":
        await recorder.hover(cached.target)
    elif cached.action == "type":
        input_text = step.enter_text.text if step.enter_text is not None else cached.input_text
        if input_text is None:
            raise RenderError(step_message("brak zamrożonego tekstu — uruchom `compile`"))
        await recorder.enter_text(cached.target, input_text)
    elif cached.action == "select":
        if step.select is None:
            raise RenderError(step_message("brak opcji dla akcji select — uruchom `compile`"))
        try:
            await recorder.select(
                cached.target,
                step.select.option,
                native=select_mode(step, scenario.config) == "native",
            )
        except (SelectDriveError, SelectsNotReadyError) as exc:
            # No silent fallback to ``select_option``: that would restore exactly
            # the invisible magic this feature removes, and unobservably. The
            # banner is what makes the loud failure legible — it names the line
            # of the scenario the author has to edit, not just the widget.
            raise RenderError(step_message(str(exc))) from exc
    elif cached.action == "highlight":
        if step.highlight is None:
            raise RenderError(
                step_message(
                    "sidecar mówi `highlight`, a krok scenariusza nim nie jest "
                    "— uruchom `compile --force`"
                )
            )
        await recorder.highlight(cached.target, step.highlight.resolved(scenario.config.highlight))
    elif cached.action == "waitFor":
        timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        try:
            await recorder.wait_for(cached.target, cached.state or "visible", timeout)
        except PlaywrightTimeoutError as exc:
            # The one absence signal a frozen gate can give: its wait window
            # elapsed. On a required step the timeout still fails the render.
            if not optional:
                raise
            raise _OptionalAbsent(f"upłynął czas oczekiwania ({timeout}s)") from exc

    expect = cached.expect
    if resolved is not None:
        # Mirror compile: the action reveals whether it navigated, and only then
        # is the entry complete enough to replace the pending one on disk.
        url_after = action_frame.url if not page.is_closed() else url_before
        expect = heuristic_expect(url_before, url_after)
        cached = _freeze_resolved(step, kind, resolved, expect, scenario_hash)
        if on_resolved is not None:
            on_resolved(index, cached)

    if not page.is_closed():
        try:
            await recorder.apply_readiness(expect)
        except PlaywrightError:
            if not page.is_closed():
                raise
    return opened
