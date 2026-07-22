"""Which page is live, and what is expected to be painted on it.

Four small stateless questions the render loop and one step handler ask about a
page: which of main/popup is currently active, whether the legacy in-DOM chrome
bar should be there, whether a page is the shell, and how a ``navigate`` step
should drive the address pill.

They sit together because they are pure predicates over already-known state — no
browser round-trip, nothing to await — which is what keeps them out of both
:mod:`~guidebot_recorder.recorder.render.popup_session` (records and lifecycle)
and :mod:`~guidebot_recorder.recorder.render.visuals` (mounting layers).
"""

from __future__ import annotations

from playwright.async_api import Page

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.models.config import ChromeConfig

from .errors import RenderError
from .popup_session import _PopupSession


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
    the shell (``install_shell`` / the shell branch of
    ``visuals._ensure_visuals``), which is independent of this flag; the cursor
    overlay is always expected.
    """

    return chrome is not None and not bare_popups


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
