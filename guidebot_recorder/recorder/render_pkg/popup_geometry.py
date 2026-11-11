"""Skąd wiadomo, jak duże naprawdę było okno popupu.

Trzystopniowy łańcuch świadków (`window.open` → bounding box zawartości →
`cropdetect`) plus konwersja z pikseli CSS na piksele nagrania. Cała zawartość
tego modułu jest testowalna bez przeglądarki poza trzema sondami `evaluate`.
"""

from __future__ import annotations

import asyncio
import math
import sys
from importlib.resources import files
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page
from tqdm import tqdm

from guidebot_recorder.models.config import Viewport
from guidebot_recorder.recorder.render_pkg.state import _PopupSession
from guidebot_recorder.video.mux import detect_content_crop


# The site's own ``window.open(url, name, "width=...,height=...")`` request is
# the most deterministic statement of how big the popup window really is, and
# Chromium honours it: a popup opened with size features gets exactly that
# viewport, *not* the context's (verified — ``new_context(viewport=...)`` does
# not override the request; only a featureless ``window.open`` inherits the
# context viewport, which is what levels 2 and 3 exist for). It is read here
# rather than from the popup because it is available the moment the popup
# opens, before its document has committed. Record the request on the *opener*
# frame; the popup is matched to it right after it opens.
#
# Note the unit: this is the popup's viewport in **CSS px**, which is only the
# same as its size in the *recording* when the compositor draws at scale 1. See
# :func:`_recording_scale` for the conversion and for why headed renders differ.
_POPUP_REQUEST_KEY = "__guidebot_popup_request"

# Set unconditionally the moment ``window.open`` runs, features or not. This is
# what tells a real ``target="_blank"`` tab (no call at all) apart from a
# featureless ``window.open(url, name)`` (a call that leaves ``KEY`` at
# ``null``, exactly like never having been called) — a distinction
# ``_POPUP_REQUEST_KEY`` alone cannot make. See ``_popup_window_opened``.
_POPUP_OPENED_KEY = "__guidebot_popup_opened"

# How long the opener's frames get to answer the geometry probe. They read a
# value the page already wrote synchronously, so a healthy frame answers in
# milliseconds; the budget exists purely to survive frames that can never
# answer at all (see ``_scan_frames_for_window_request``).
_POPUP_REQUEST_LOOKUP_TIMEOUT = 2.0

# Absolute ceiling on the lookup, cleanup included. The scan bounds itself, so
# reaching this means the bounding itself misbehaved; it exists so that no page
# can ever hang a render here.
_POPUP_REQUEST_HARD_TIMEOUT = 5.0


def _load_js(name: str) -> str:
    """Wczytaj skrypt strony z ``render_pkg/js`` — ta sama konwencja co
    ``overlay/cursor.js``, ``chrome/chrome.js`` czy ``selects/selects.js``.
    """

    return files("guidebot_recorder.recorder.render_pkg.js").joinpath(name).read_text("utf-8")


#: Nazwy kluczy wstrzykiwane do ``popup_request.js``. Skrypt trzyma je jako
#: sentinele zamiast interpolacji f-stringiem: dzięki temu plik jest poprawnym
#: JS-em (lintowalnym, kolorowanym), a nawiasy klamrowe nie muszą być podwajane.
_POPUP_REQUEST_SCRIPT = (
    _load_js("popup_request.js")
    .replace("__GUIDEBOT_REQUEST_KEY__", _POPUP_REQUEST_KEY)
    .replace("__GUIDEBOT_OPENED_KEY__", _POPUP_OPENED_KEY)
)


def _parse_window_request(requested: object) -> tuple[int, int] | None:
    """Validate one ``window.open`` geometry record read back from the page."""
    if not isinstance(requested, dict):
        return None
    width, height = requested.get("width"), requested.get("height")
    if not isinstance(width, int | float) or not isinstance(height, int | float):
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _discard_pending(task: asyncio.Future) -> None:
    """Cancel a lookup we no longer need, and never orphan its exception.

    A cancelled ``Frame.evaluate`` can still settle later — typically with
    ``Frame was detached`` — and an unread exception on an abandoned future is
    what produces asyncio's "Future exception was never retrieved" noise.
    Retrieving it in a done-callback keeps that silent.
    """

    if task.done():
        # Already settled, so there is nothing to cancel — but a probe that
        # raised and was never read is orphaned just the same. Read it.
        if not task.cancelled():
            task.exception()
        return
    task.cancel()
    task.add_done_callback(lambda done: done.cancelled() or done.exception())


async def _frame_window_request(frame: Frame) -> tuple[int, int] | None:
    """Read one frame's ``window.open`` record, or ``None`` if it cannot answer."""

    try:
        requested = await frame.evaluate(f"() => window.{_POPUP_REQUEST_KEY} || null")
    except PlaywrightError:
        # The frame navigated away, detached, or never ran the init script.
        return None
    return _parse_window_request(requested)


async def _scan_frames_for_window_request(opener: Page) -> tuple[int, int] | None:
    """Ask every frame for its record *concurrently* and return the first answer.

    Concurrency is the point, not an optimisation. ``Frame.evaluate`` accepts no
    timeout, and an ad-heavy page routinely carries iframes whose document never
    commits an execution context — evaluating on one of those never returns.
    Asking the frames one after another therefore lets a single dead ad iframe
    block the render forever (and it did).

    Asking them all at once is only half the fix, though: waiting for *all* the
    probes means a dead frame still costs the whole
    ``_POPUP_REQUEST_LOOKUP_TIMEOUT`` even when a healthy frame answered in
    milliseconds — and because this runs on every popup open, that budget shows
    up in the finished film as a dead pause on the popup. So the scan settles
    the moment it has a usable answer and abandons the rest. The budget survives
    unchanged as what it was always meant to be: the ceiling for the case where
    *nobody* ever answers, after which the caller falls back to the content
    bounding box (and then to cropdetect).

    Which answer wins when several frames have one: the probes are examined in a
    fixed priority order — the top document first, then the remaining frames
    newest-first — and the highest-priority *already-settled* answer is taken.
    That is safe because the answers are not really independent. The init script
    republishes every record on the real top document, so a same-origin page has
    one record read back from several places; a cross-origin opener frame that
    could not republish is instead the only frame holding one, and the top
    answers ``None``. Genuinely conflicting records would need two frames to
    each call ``window.open`` with different sizes before this runs, and the
    scan runs immediately after the popup opens — newest-first is then exactly
    the record that opened it. Worst case the crop rect is one popup stale,
    which the downstream fallbacks already tolerate.
    """

    top = opener.main_frame
    frames = [top, *(frame for frame in reversed(opener.frames) if frame is not top)]
    tasks = [asyncio.ensure_future(_frame_window_request(frame)) for frame in frames]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POPUP_REQUEST_LOOKUP_TIMEOUT
    try:
        pending = set(tasks)
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # Wake on every settled probe rather than on the slowest one. A
            # frame that raises simply settles without an answer: it retires
            # from ``pending`` and the others carry on.
            _, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception() is None:
                    if (size := task.result()) is not None:
                        return size
        return None
    finally:
        for task in tasks:
            _discard_pending(task)


async def _popup_window_request(opener: Page) -> tuple[int, int] | None:
    """Return the window size the *opener* asked ``window.open`` for, if any.

    Bounded twice over, because this runs at the worst possible moment — right
    after a popup opens, while the opener is at its busiest — and a render that
    hangs here is unrecoverable. An unknown size is by contrast harmless: it
    means no crop, i.e. the full-canvas behaviour that predates popup cropping.
    So the lookup always terminates, and says so when it gives up.
    """

    lookup = asyncio.ensure_future(_scan_frames_for_window_request(opener))
    try:
        size = await asyncio.wait_for(asyncio.shield(lookup), _POPUP_REQUEST_HARD_TIMEOUT)
    except TimeoutError:
        # ``shield`` means the timeout does not itself cancel the lookup, so it
        # is still ours to dispose of cleanly.
        _discard_pending(lookup)
        tqdm.write(
            "OSTRZEŻENIE: odczyt rozmiaru okna popupu nie zakończył się w "
            f"{_POPUP_REQUEST_HARD_TIMEOUT:g}s — popup bez przycięcia",
            file=sys.stderr,
        )
        return None
    if size is None:
        tqdm.write(
            "OSTRZEŻENIE: żadna ramka nie podała rozmiaru okna z window.open() "
            "— popup bez przycięcia",
            file=sys.stderr,
        )
    return size


async def _frame_window_opened(frame: Frame) -> bool | None:
    """Read one frame's "was window.open called" flag, or ``None`` if it cannot answer."""

    try:
        return bool(await frame.evaluate(f"() => window.{_POPUP_OPENED_KEY} === true"))
    except PlaywrightError:
        # The frame navigated away, detached, or never ran the init script.
        return None


async def _scan_frames_for_window_opened(opener: Page) -> bool | None:
    """Ask every frame, concurrently, whether it recorded a ``window.open`` call.

    Mirrors ``_scan_frames_for_window_request``: the ``OPENED`` flag is mirrored
    to the real top document only as a same-origin optimisation (see
    ``_POPUP_REQUEST_SCRIPT``), so a call made from a genuinely cross-origin
    frame — routine for the shell's site iframe — never reaches that mirror,
    and the top frame alone cannot be trusted to answer. Every frame is asked
    directly instead, concurrently and for the same reason as the geometry
    scan: a dead ad iframe whose document never commits an execution context
    must not block the others, or the render, forever.

    Returns ``True`` the moment any frame reports the flag set, ``False`` if
    every frame answered and none did, or ``None`` if no frame could answer at
    all (the opener navigated away or died) — callers must treat ``None`` as
    unknown, not as "no popup".
    """

    top = opener.main_frame
    frames = [top, *(frame for frame in reversed(opener.frames) if frame is not top)]
    tasks = [asyncio.ensure_future(_frame_window_opened(frame)) for frame in frames]
    try:
        await asyncio.wait(tasks, timeout=_POPUP_REQUEST_LOOKUP_TIMEOUT)
        answered = False
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is None:
                result = task.result()
                if result is True:
                    return True
                if result is False:
                    answered = True
        return False if answered else None
    finally:
        for task in tasks:
            _discard_pending(task)


async def _popup_window_opened(page: Page) -> bool:
    """Whether this document called ``window.open`` at all, features or not.

    ``_popup_window_request`` answers "what geometry did the site ask for", and
    returns ``None`` both for a featureless ``window.open(url, name)`` and for a
    window this document never opened. Only the second is a ``target="_blank"``
    tab, and telling them apart is possible *while the popup is alive* — unlike
    the crop chain, whose verdict arrives after the recording is finished.

    Read per frame, not just the top document: the init script's mirror onto
    the real top document (``realTop[OPENED] = true``) is only a same-origin
    optimisation — it throws and is swallowed for a genuinely cross-origin
    frame, which is routine for the shell's site iframe. Reading only the top
    frame therefore misses exactly that call and misreports a real popup as a
    ``target="_blank"`` tab. ``_scan_frames_for_window_opened`` asks every
    frame instead, exactly as ``_popup_window_request`` already does for the
    geometry record.

    Bounded twice over, exactly like ``_popup_window_request``: this runs at
    the same worst possible moment — right after a popup opens, while the
    opener is at its busiest — and a render that hangs here is just as
    unrecoverable. An unknown answer is by contrast harmless: it means
    "assume a popup", i.e. the fail-safe direction that never mounts an
    address bar on uncertainty. So the lookup always terminates, and says so
    when it gives up.
    """

    def _assume_opened() -> bool:
        tqdm.write(
            "OSTRZEŻENIE: żadna ramka strony otwierającej nie potwierdziła wywołania "
            "window.open() — zakładam otwarty popup (bez paska adresu)",
            file=sys.stderr,
        )
        return True

    lookup = asyncio.ensure_future(_scan_frames_for_window_opened(page))
    try:
        opened = await asyncio.wait_for(asyncio.shield(lookup), _POPUP_REQUEST_HARD_TIMEOUT)
    except TimeoutError:
        # ``shield`` means the timeout does not itself cancel the lookup, so it
        # is still ours to dispose of cleanly.
        _discard_pending(lookup)
        tqdm.write(
            "OSTRZEŻENIE: sprawdzenie wywołania window.open() nie zakończyło się w "
            f"{_POPUP_REQUEST_HARD_TIMEOUT:g}s — zakładam otwarty popup (bez paska adresu)",
            file=sys.stderr,
        )
        return True
    except PlaywrightError:
        # An opener that navigated or died reports nothing; treat it as "unknown",
        # which keeps today's bare-popup behaviour rather than mounting a bar.
        return _assume_opened()
    if opened is None:
        return _assume_opened()
    return opened


#: A content bounding box at least this fraction of the viewport in *both*
#: dimensions is refused as degenerate. ``body``'s children can trivially fill
#: the frame (a ``100vw/100vh`` wrapper, a full-bleed background), in which case
#: the box merely restates the context viewport this whole mechanism exists to
#: correct, and level 3 has to decide instead.
POPUP_BBOX_DEGENERATE_RATIO = 0.98

#: How long the popup's content measurement gets to come back. The walk is
#: bounded in the page (see ``_POPUP_CONTENT_BOX_SCRIPT``), so a healthy popup
#: answers in single-digit milliseconds; the budget exists for the same reason
#: the ``window.open`` lookup has one — ``Page.evaluate`` accepts no timeout, and
#: a document that never commits an execution context never answers at all.
_POPUP_CONTENT_BOX_TIMEOUT = 2.0

#: Level 2: measure the popup's painted content from inside the popup document.
#: Deliberately skips ``documentElement`` and ``body`` — both are laid out
#: against the *context* viewport, so both report the very number being
#: corrected. Only ``body``'s children are measured, and a child that paints
#: nothing itself (a transparent layout wrapper) is descended into rather than
#: counted, so a full-width wrapper around a small dialog does not inflate the
#: result. Guidebot's own injected overlays (``data-guidebot-*``) are skipped:
#: the cursor lives in the popup too and would stretch the box to wherever it
#: happens to sit. A document that paints its own page background declines
#: outright — see ``paintsPage`` below.
_POPUP_CONTENT_BOX_SCRIPT = _load_js("popup_content_box.js")


def _parse_content_box(measured: object) -> tuple[int, int, int, int] | None:
    """Validate and gate one content bounding box read back from a popup.

    Returns ``(width, height, x, y)`` in recording pixels, or ``None`` when the
    measurement is unusable — non-finite, empty, or *degenerate*: at least
    :data:`POPUP_BBOX_DEGENERATE_RATIO` of the viewport in **both** dimensions,
    which means the content genuinely is full-bleed and the box says nothing the
    uncropped canvas did not already say. The origin rounds down and the far
    edges round up, so rounding never shaves a painted pixel.
    """
    if not isinstance(measured, dict):
        return None
    values: list[float] = []
    for key in ("x", "y", "width", "height", "viewportWidth", "viewportHeight"):
        value = measured.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(float(value)):
            return None
        values.append(float(value))
    x, y, width, height, viewport_width, viewport_height = values
    if width <= 0 or height <= 0 or viewport_width <= 0 or viewport_height <= 0:
        return None
    if (
        width >= viewport_width * POPUP_BBOX_DEGENERATE_RATIO
        and height >= viewport_height * POPUP_BBOX_DEGENERATE_RATIO
    ):
        return None
    left, top = math.floor(x), math.floor(y)
    return (
        math.ceil(x + width) - left,
        math.ceil(y + height) - top,
        max(0, left),
        max(0, top),
    )


async def _popup_content_box(popup: Page) -> tuple[int, int, int, int] | None:
    """Measure the popup's painted content while the page is still alive.

    Level 2 of the crop chain, used when the site opened the popup without size
    features. A closed page, a navigation race or a hostile document degrades to
    ``None`` (fall through to level 3) rather than failing the render.
    """
    try:
        measured = await popup.evaluate(_POPUP_CONTENT_BOX_SCRIPT)
    except PlaywrightError:
        return None
    return _parse_content_box(measured)


def _start_popup_content_box(popup: Page) -> asyncio.Task[tuple[int, int, int, int] | None]:
    """Begin measuring the popup off the render's critical path.

    The measurement has to happen while the popup's DOM exists, and the DOM dies
    long before composition — but awaiting it where it starts would put its
    latency *inside the recorded timeline*, right at the moment the popup appears
    on camera. So it is started here and collected later
    (:func:`_settle_popup_content_box`), by which time it has long since
    finished: the render does not block on it at all.
    """

    return asyncio.ensure_future(_popup_content_box(popup))


async def _settle_popup_content_box(popup: _PopupSession) -> None:
    """Collect the pending measurement, bounded, before the context goes away.

    Must run while the browser context is still open — a closed context can only
    answer with "target closed". Bounded for the same reason the ``window.open``
    lookup is: ``Page.evaluate`` never times out on its own, and no popup may
    hang a render. Giving up costs the crop, not the video.
    """

    probe = popup.content_box_probe
    if probe is None:
        return
    popup.content_box_probe = None
    try:
        popup.content_box = await asyncio.wait_for(
            asyncio.shield(probe), _POPUP_CONTENT_BOX_TIMEOUT
        )
    except TimeoutError:
        # ``shield`` keeps the timeout from cancelling the probe itself, so it is
        # still ours to dispose of without orphaning its exception.
        _discard_pending(probe)
        tqdm.write(
            "OSTRZEŻENIE: pomiar zawartości popupu nie zakończył się w "
            f"{_POPUP_CONTENT_BOX_TIMEOUT:g}s — przycięcie z heurystyki obrazu",
            file=sys.stderr,
        )
    except PlaywrightError:
        # The popup closed under the probe: level 3 decides instead.
        popup.content_box = None


def _page_viewport(page: Page) -> tuple[int, int] | None:
    """*page*'s layout viewport as a plain pair, or ``None`` when unset."""
    viewport = page.viewport_size
    if viewport is None:
        return None
    return viewport["width"], viewport["height"]


def _log_popup_crop(level: str, crop: tuple[int, int, int, int] | None, verbose: bool) -> None:
    """Say which fallback level framed the popup — silent degradation is a bug."""
    if not verbose:
        return
    if crop is None:
        tqdm.write(f"   ⤷ kadr popupu: {level} nic nie wykrył — pełne płótno nagrania")
        return
    width, height, x, y = crop
    tqdm.write(f"   ⤷ kadr popupu: {level} → {width}x{height}+{x}+{y}")


#: Backing scales a compositor plausibly renders a window at. Headless Chromium
#: draws at 1; a headed window inherits its screen's scale factor, which is 2 on
#: every shipping HiDPI display and 3 only on the densest mobile-class panels.
#: Used to sanity-check a *measured* recording scale, never to pick one.
_PLAUSIBLE_BACKING_SCALES = (1, 2, 3)

#: How far a measured recording scale may sit from the nearest scale the fit
#: rule can produce. Wide enough for the recorder's even-pixel snapping (a 597px
#: window records as 596), far too narrow to admit an unrelated rect.
_RECORDING_SCALE_TOLERANCE = 0.02


def _recording_scale(
    measured: tuple[int, int, int, int] | None,
    viewport: tuple[int, int] | None,
    canvas: tuple[int, int] | None,
) -> float | None:
    """How many recording pixels the popup got per CSS pixel, or ``None``.

    Playwright records every page in a context onto one canvas of
    ``record_video_size``, and fits each page's *compositor* frame into it
    preserving aspect ratio, anchored top-left, padding the rest. That frame is
    in **device** pixels: a headed browser on a HiDPI screen composites at the
    screen's backing scale, so a 500x670 popup arrives as 1000x1340 and is fitted
    into a 1376x800 canvas at 0.597 — leaving the window occupying 597x800
    *recording* pixels, not 500x670. Headless composites at 1 and the same popup
    records 1:1, which is why this only ever bit headed renders.

    The backing scale is not observable from the page (``devicePixelRatio``
    reports the emulated 1) nor from ``screenshot(scale="device")``, so it is
    read back off the recording: *measured* is the popup's rendered rect as
    trimmed out of the padding, and the scale is its width over the viewport's.

    The measurement is a pixel heuristic, so it is only believed when it looks
    like a fitted window: anchored at the origin, aspect preserved, and within
    :data:`_RECORDING_SCALE_TOLERANCE` of a scale the fit rule can actually
    produce for some plausible backing scale. Anything else — a page whose own
    background matched the padding colour, ink mistaken for a window — returns
    ``None``, i.e. "assume 1:1", which is the behaviour that predates this.
    """
    if measured is None or viewport is None or canvas is None:
        return None
    width, height, x, y = measured
    viewport_width, viewport_height = viewport
    if (x, y) != (0, 0) or viewport_width <= 0 or viewport_height <= 0:
        return None
    if width <= 0 or height <= 0:
        return None
    scale = width / viewport_width
    if abs(height / viewport_height - scale) > _RECORDING_SCALE_TOLERANCE * scale:
        return None  # not a uniformly scaled window
    fitted = [
        min(backing, canvas[0] / viewport_width, canvas[1] / viewport_height)
        for backing in _PLAUSIBLE_BACKING_SCALES
    ]
    if not any(
        abs(scale - candidate) <= _RECORDING_SCALE_TOLERANCE * candidate for candidate in fitted
    ):
        return None
    return scale


def _scale_rect(rect: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    """Map a CSS-pixel ``(width, height, x, y)`` into recording pixels.

    The origin floors and the far edges ceil, so the rounding can only ever add
    a fringe pixel of padding — never shave one off the popup's own ink.
    """
    width, height, x, y = rect
    left, top = math.floor(x * scale), math.floor(y * scale)
    return (
        math.ceil((x + width) * scale) - left,
        math.ceil((y + height) * scale) - top,
        left,
        top,
    )


def _resolve_popup_crop(
    *,
    window_size: tuple[int, int] | None,
    content_box: tuple[int, int, int, int] | None,
    popup_video: Path,
    verbose: bool,
    viewport: tuple[int, int] | None = None,
    canvas: tuple[int, int] | None = None,
) -> tuple[tuple[int, int, int, int] | None, str]:
    """Pick the popup crop rect from the best available witness.

    Three levels, most deterministic first, returned with the name of the one
    that answered so the log can say which framed the video:

    1. ``window.open`` size features — what the site literally asked for.
    2. the popup's content bounding box — measured in the page, gated by
       :data:`POPUP_BBOX_DEGENERATE_RATIO`.
    3. ``cropdetect`` over the recording — a pixel heuristic, last resort.

    Levels 1 and 2 speak **CSS pixels** and the crop is applied to the
    *recording*, which are not the same unit — see :func:`_recording_scale`. The
    same padding-trimming pass that backs level 3 supplies the conversion factor
    whenever there is a *viewport* and a *canvas* to correct between; without
    them, or when the measurement does not look like a fitted window, the rects
    pass through as-is (1:1, which is what headless produces).

    All three declining yields ``(None, "none")``: the pre-crop full-canvas
    filtergraph, byte for byte.
    """
    css_evidence = window_size is not None or content_box is not None
    measured = (
        detect_content_crop(popup_video)
        if not css_evidence or (viewport is not None and canvas is not None)
        else None
    )
    if window_size is not None or content_box is not None:
        scale = _recording_scale(measured, viewport, canvas)
        if verbose and scale is not None and abs(scale - 1.0) > _RECORDING_SCALE_TOLERANCE:
            tqdm.write(f"   ⤷ nagranie popupu w skali {scale:.3f}× — kadr przeliczony")
        if window_size is not None:
            # The measured rect *is* the whole window in recording pixels, so
            # prefer it over rescaling the request: it needs no rounding and
            # cannot leave a seam of padding along the right or bottom edge.
            crop = measured if scale is not None else (window_size[0], window_size[1], 0, 0)
            assert crop is not None
            _log_popup_crop("window.open", crop, verbose)
            return crop, "window.open"
        assert content_box is not None
        crop = content_box if scale is None else _scale_rect(content_box, scale)
        _log_popup_crop("bbox", crop, verbose)
        return crop, "bbox"
    _log_popup_crop("cropdetect", measured, verbose)
    return (measured, "cropdetect") if measured is not None else (None, "none")


def _popup_fills_canvas(popup_crop: tuple[int, int, int, int] | None, viewport: Viewport) -> bool:
    """Whether the popup window occupies the entire recording canvas.

    ``None`` is the ``_blank`` tab case: every crop level declined, so no witness
    could name a window smaller than the recording. An explicit rect covering the
    canvas at the origin says the same thing positively — a ``window.open`` that
    asked for the full viewport.

    The distinction matters because ``float`` insets the popup at
    :attr:`PopupConfig.scale`; applied to a full viewport that reads as a shrunken
    clone of the page rather than as a separate window.

    ``viewport`` here is *not* CSS pixels despite the name: ``cfg.viewport``'s
    width/height are passed verbatim as ``record_video_size`` when the browser
    context is created (see ``render.py`` around the ``browser.new_context`` call),
    so it already is the canvas measured in recording pixels — the same unit
    ``popup_crop`` is in. No CSS-to-recording-pixel conversion belongs here.
    """

    if popup_crop is None:
        return True
    width, height, x, y = popup_crop
    return (x, y) == (0, 0) and (width, height) == (viewport.width, viewport.height)
