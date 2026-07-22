"""Mounting, priming and handing over the injected visual layers.

Everything that puts guidebot's own pixels on a page and keeps them there: the
desktop-opener choreography, the atomic cursor+chrome mount
(:func:`_ensure_visuals`), the bounded prime loop that survives Chromium replacing
a fresh ``about:blank`` document, and the two halves of the popup cursor
hand-over.

:func:`_prepare_main_after_popup_close` lives here rather than in
:mod:`~guidebot_recorder.recorder.render.popup_session` for two reasons: it is the
exact reverse of :func:`_hand_cursor_to_popup` and reads three lines of popup
state, and putting it beside the session records would close an import cycle (this
module already needs those records for its own signatures).

Two test seams meet here. ``Recorder`` is name-imported because
:func:`_prepare_main_after_popup_close` constructs one and a patch on *this*
module is what has to reach it; the same class is also constructed in
:mod:`~guidebot_recorder.recorder.render._run`, so replacing it takes **two** patch
lines, not one. ``_prepare_main_after_popup_close`` itself is the other seam —
defined here, called through this module object from ``_run``.

``Overlay``, by contrast, is annotated through its module object: it is a seam
patched on ``_run``, nothing here calls it, and a name-import would be an
unreachable copy.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Page

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.desktop import DesktopOverlay
from guidebot_recorder.overlay import overlay as overlay_module
from guidebot_recorder.recorder.recorder import Recorder

from .constants import _POPUP_DETECTION_SECONDS, _POPUP_QUIESCENCE_SECONDS
from .errors import RenderError
from .pages import _is_shell_page
from .popup_session import _PopupSession

#: Desktop-opener beats, in ms. The window growth (`_DESKTOP_OPEN_MS`) is the one
#: the eye actually reads; the rest are short settles that keep the double-click
#: legible without dragging the opener out.
_DESKTOP_SETTLE_MS = 260
_DESKTOP_DOUBLE_CLICK_GAP_MS = 130
_DESKTOP_PRE_OPEN_MS = 220
_DESKTOP_OPEN_MS = 760


async def _play_desktop_opener(
    desktop: DesktopOverlay,
    overlay: overlay_module.Overlay,
    page: Page,
    payload: dict[str, str],
    *,
    hold: float,
    settle_ms: float,
    reveal: Callable[[], Awaitable[None]],
    on_click: Callable[[str], None] | None = None,
) -> None:
    """Paint a desktop, arc the cursor to its icon, double-click, open the window.

    The visual half of a :class:`~guidebot_recorder.models.scenario.Desktop` step.
    Ends on the revealed underlay (the chrome shell) — *reveal* is called with the
    faux window still covering it, so hiding the desktop overlay uncovers a live
    browser rather than a blank frame. ``on_click`` (when given) stamps a click
    SFX at each of the two clicks.

    Fails loud if the icon cannot be located: the desktop was just painted, so a
    missing icon is a real overlay bug, not a case to paper over.
    """
    await desktop.show(page, payload)
    await overlay.show(page)
    center = await desktop.icon_center(page)
    if center is None:
        raise RenderError("nie udało się zlokalizować ikony pulpitu po jej narysowaniu")
    await overlay.move_to(page, center[0], center[1])
    await page.wait_for_timeout(max(settle_ms, _DESKTOP_SETTLE_MS))
    # Double-click: two ripples a beat apart, each with its own click SFX.
    await overlay.ripple(page)
    if on_click is not None:
        on_click("click")
    await page.wait_for_timeout(_DESKTOP_DOUBLE_CLICK_GAP_MS)
    await overlay.ripple(page)
    if on_click is not None:
        on_click("click")
    await page.wait_for_timeout(_DESKTOP_PRE_OPEN_MS)
    await desktop.open_window(page, _DESKTOP_OPEN_MS)
    await page.wait_for_timeout(_DESKTOP_OPEN_MS)
    # Uncover the live browser: show the underlay first, THEN drop the overlay, so
    # the reveal never flashes a blank document between the two.
    await reveal()
    await desktop.hide(page)
    if hold > 0:
        await page.wait_for_timeout(int(hold * 1000))


async def _ensure_visuals(
    page: Page,
    overlay: overlay_module.Overlay,
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
    try:
        readiness = await page.evaluate(ensure_script, args)
    except PlaywrightError as exc:
        # A prior click can trigger a navigation/reload the recorder never asked
        # for — e.g. a cookie banner whose accept runs `window.location.reload()`,
        # or a link that leaves for an external login. The next step's visual
        # mount then races the in-flight document swap and the context is
        # destroyed. Wait for the replacement document and retry once, re-reading
        # the URL for the fresh context. Mirrors the opener-settle retry in
        # `_prepare_main_after_popup_close`. A closed page is a real failure.
        if page.is_closed():
            raise RenderError("okno zamknęło się w trakcie montażu warstw wizualnych") from exc
        await page.wait_for_load_state()
        args[3] = page.url
        readiness = await page.evaluate(ensure_script, args)
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
    overlay: overlay_module.Overlay,
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
    overlay: overlay_module.Overlay,
    chrome: Chrome | None,
    settle_ms: float,
    restore_cursor_to: tuple[float, float] | None = None,
) -> None:
    """Let opener navigation settle before touching its execution context again.

    This is the single funnel for "the popup is gone, the main window is active
    again", so it is also where the cursor handed over to the popup by
    :func:`_hand_cursor_to_popup` is handed back.

    ``restore_cursor_to`` undoes that hand-over's side effect: centring the
    cursor in the popup moved the *shared* :attr:`Overlay.pos`, so without this
    the reappearing main-window cursor would jump to wherever the popup's centre
    happened to be rather than the control it last used.
    """

    if restore_cursor_to is not None:
        overlay.pos = restore_cursor_to
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
    await overlay.show(page)


async def _hand_cursor_to_popup(
    main_page: Page, popup: _PopupSession, overlay: overlay_module.Overlay
) -> None:
    """Move the cursor into the popup, centred, for as long as it is on screen.

    The cursor is a DOM element injected into every top-level document, so a
    popup mounts its *own* instance — leaving two cursors alive at once. The
    compositor keeps the main window's video visible around/behind the popup
    (and fully visible for a floating popup), so the stale main-window cursor
    would be recorded next to the live one. Only the active window paints a
    cursor; :func:`_prepare_main_after_popup_close` reverses this.

    ``hide`` is a per-page call on purpose — a context-wide init-script flag
    (like ``barePopups``) cannot target one window.

    The popup's own cursor is then parked at the centre of its viewport and
    revealed. Without this it would inherit :attr:`Overlay.pos` — coordinates
    from the *main* window, typically the opener control in a corner — and stay
    invisible there until the first action moved it. ``ms=0`` because a glide
    would be a slide in from wherever the other window's cursor happened to be,
    which reads as motion that never occurred.
    """

    if main_page.is_closed():
        return
    try:
        await overlay.hide(main_page)
    except PlaywrightError:
        # The opener can navigate/close under us while the popup is opening;
        # the loop's own lifecycle checks report that authoritatively.
        if not main_page.is_closed():
            raise
    # Remembered so closing the popup does not leave the main window's cursor
    # parked at the popup's centre: ``Overlay.pos`` is shared across pages.
    popup.main_cursor_pos = overlay.pos
    viewport = popup.page.viewport_size
    if viewport is None or popup.page.is_closed():
        return
    try:
        await overlay.move_to(popup.page, viewport["width"] / 2, viewport["height"] / 2, ms=0)
        await overlay.show(popup.page)
    except PlaywrightError:
        # A popup that dies while being furnished is the loop's business, not
        # the cursor's; losing the centring costs a cosmetic, never the render.
        if not popup.page.is_closed():
            raise
