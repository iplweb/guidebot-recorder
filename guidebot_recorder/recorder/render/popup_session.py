"""The deterministic page contract: one main window, at most one popup.

Holds the two observation records — :class:`_PageObservation` for every page the
context ever opened, :class:`_PopupSession` for the one popup a render may have —
and the lifecycle helpers that keep them honest: which pages are outside the
contract, when the popup really closed, and how a freshly opened page is
furnished.

The *visual* half of that lifecycle (mounting layers, handing the cursor over and
back) lives in :mod:`~guidebot_recorder.recorder.render.visuals`, which imports
this module. The dependency runs one way on purpose; reversing it would close a
cycle.

``Overlay`` is annotated through its module object rather than name-imported. It
is a test seam whose patch lands on
:mod:`~guidebot_recorder.recorder.render._run`, and a bare ``from ... import
Overlay`` here would be an import-time copy no patch can reach — harmless while
only an annotation uses it, a silent trap the moment somebody calls it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Page, Video

from guidebot_recorder.chrome import Chrome
from guidebot_recorder.overlay import overlay as overlay_module

from .constants import _POPUP_DETECTION_SECONDS, _POPUP_QUIESCENCE_SECONDS


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
    main_cursor_pos: tuple[float, float] | None = None
    """Where the main window's cursor stood before the popup took it over.

    :attr:`Overlay.pos` is shared by every page, so centring the cursor in the
    popup overwrites the opener's. Restored on close.
    """
    is_blank_tab: bool = False
    """Whether this window is a real browser tab rather than a popup window.

    True when the opener never called ``window.open`` at all — the
    ``target="_blank"`` case. Decided at furnishing time via
    ``popup_detect._popup_window_opened``, because the crop chain's verdict does
    not exist until the recording is over.

    Do **not** substitute ``popup_crop is None`` for this. That is a weaker
    signal: a *featureless* ``window.open`` whose page paints a full-bleed
    background also declines every crop level, and it is a genuine floating
    window that must keep the ``float`` presentation (pinned by
    ``test_full_bleed_featureless_popup_renders_uncropped``).
    """
    wants_bar: bool = False
    """Whether this window shows the legacy in-DOM address bar.

    True only for a real ``target="_blank"`` tab — a browser tab with no address
    bar reads as a rendering fault. Every other popup stays bare and is framed by
    the compositor instead. Decided at open time from
    ``popup_detect._popup_window_opened``, because the crop chain's verdict does
    not exist until the recording is over, and the bar is painted DOM that would
    corrupt crop levels 2 and 3.
    """
    window_size: tuple[int, int] | None = None
    """The popup's real window size in CSS px, or ``None`` when unknown.

    Playwright gives every page in the context the *same* recording canvas (the
    ``record_video_size`` handed to ``browser.new_context`` in ``_run``), so the
    popup's MP4 is main-viewport sized no matter how small a window the site
    asked for. This is the geometry the compositor crops that canvas back down
    to.
    """
    viewport: tuple[int, int] | None = None
    """The popup page's own layout viewport in CSS px, or ``None`` if unknown.

    Not the same thing as :attr:`window_size` (which the site *asked* for) nor as
    the recording canvas: it is what the popup actually got, and it is the unit
    levels 1 and 2 measure in. Paired with the canvas it lets
    ``popup_crop._recording_scale`` convert those measurements into recording
    pixels.
    """
    content_box: tuple[int, int, int, int] | None = None
    """The popup's painted content as ``(width, height, x, y)``, or ``None``.

    Level 2 of the crop chain — what a featureless ``window.open(url, name)``
    leaves us with. Measured while the popup page is alive: the recording
    outlives the page, its DOM does not. The chain lives in ``popup_crop``.
    """
    content_box_probe: asyncio.Task[tuple[int, int, int, int] | None] | None = None
    """The in-flight measurement, until ``popup_crop._settle_popup_content_box``
    reaps it.

    Kept pending on purpose: starting it when the popup opens and collecting it
    at the end of the run keeps its cost out of the recorded timeline.
    """


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


async def _prepare_popup(
    page: Page,
    overlay: overlay_module.Overlay,
    chrome: Chrome | None,
    *,
    expect_chrome: bool | None = None,
    mount_bar: bool = False,
) -> bool:
    """Prepare a new page; translate close races into lifecycle state.

    ``expect_chrome`` defaults to ``chrome is not None``; pass ``False`` for a
    bare (floating) popup so the chrome bar is not mounted on it (the cursor is
    still ensured).

    ``mount_bar`` forces the legacy bar onto this one page even when the
    context-wide script is bare — a real ``target="_blank"`` tab is a browser
    tab and reads as a rendering fault without an address bar.
    """

    if expect_chrome is None:
        expect_chrome = chrome is not None
    if page.is_closed():
        return False
    try:
        await page.bring_to_front()
        await page.wait_for_load_state()
        await overlay.ensure(page)
        if chrome is not None and mount_bar:
            await chrome.install_bar(page)
        elif chrome is not None and expect_chrome:
            await chrome.ensure(page)
    except PlaywrightError:
        if page.is_closed():
            return False
        raise
    return not page.is_closed()
