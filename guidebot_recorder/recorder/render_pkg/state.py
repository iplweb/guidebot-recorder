"""Stan renderu obserwowany po stronie Pythona: strony, popup, robota TTS.

Czyste struktury danych — bez logiki i bez zależności od orkiestratora, dzięki
czemu importuje je zarówno :mod:`popup_geometry`, jak i :mod:`run`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from playwright.async_api import Page, Video

from guidebot_recorder.models.config import TtsConfig


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
    :func:`_popup_window_opened`, because the crop chain's verdict does not
    exist until the recording is over.

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
    :func:`_popup_window_opened`, because the crop chain's verdict does not exist
    until the recording is over, and the bar is painted DOM that would corrupt
    crop levels 2 and 3.
    """
    window_size: tuple[int, int] | None = None
    """The popup's real window size in CSS px, or ``None`` when unknown.

    Playwright gives every page in the context the *same* recording canvas (see
    ``record_video_size`` below), so the popup's MP4 is main-viewport sized no
    matter how small a window the site asked for. This is the geometry the
    compositor crops that canvas back down to.
    """
    viewport: tuple[int, int] | None = None
    """The popup page's own layout viewport in CSS px, or ``None`` if unknown.

    Not the same thing as :attr:`window_size` (which the site *asked* for) nor as
    the recording canvas: it is what the popup actually got, and it is the unit
    levels 1 and 2 measure in. Paired with the canvas it lets
    :func:`_recording_scale` convert those measurements into recording pixels.
    """
    content_box: tuple[int, int, int, int] | None = None
    """The popup's painted content as ``(width, height, x, y)``, or ``None``.

    Level 2 of the crop chain — what a featureless ``window.open(url, name)``
    leaves us with. Measured while the popup page is alive: the recording
    outlives the page, its DOM does not.
    """
    content_box_probe: asyncio.Task[tuple[int, int, int, int] | None] | None = None
    """The in-flight measurement, until :func:`_settle_popup_content_box` reaps it.

    Kept pending on purpose: starting it when the popup opens and collecting it
    at the end of the run keeps its cost out of the recorded timeline.
    """


@dataclass(slots=True)
class _TtsWork:
    text: str
    config: TtsConfig
    destinations: list[tuple[str, int]]
