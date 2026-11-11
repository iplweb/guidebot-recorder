"""Kontrakt „główne okno + co najwyżej jeden popup”.

Reguły mówiące, która strona jest aktywna, która pojawiła się poza kontraktem
i kiedy spodziewamy się paska adresu.
"""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import Page

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.models.config import ChromeConfig
from guidebot_recorder.recorder.render_pkg.errors import RenderError
from guidebot_recorder.recorder.render_pkg.state import _PageObservation, _PopupSession

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1


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
