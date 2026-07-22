"""What the opener asked ``window.open`` for, and whether it called it at all.

Two questions, both answered by an init script that wraps ``window.open`` on every
document, and both read back from the *opener* while it is still alive:

* the requested window geometry — level 1 of the popup crop chain, whose
  remaining levels are in :mod:`~guidebot_recorder.recorder.render.popup_crop`;
* whether ``window.open`` ran at all — which is what tells a real
  ``target="_blank"`` browser tab from a featureless ``window.open(url, name)``.

Both lookups are bounded twice over, because they run at the worst possible moment
(right after a popup opens, while the opener is at its busiest) and a render that
hangs here is unrecoverable.

``_POPUP_REQUEST_LOOKUP_TIMEOUT`` is a test seam and is defined *and* read here, so
a patch on this module reaches the scans below. Do not move it to
:mod:`~guidebot_recorder.recorder.render.constants`: that would put the value a
test replaces in one module and the globals its readers resolve in another.
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Frame, Page
from tqdm import tqdm

from .tasks import _discard_pending

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
# ``popup_crop._recording_scale`` for the conversion and for why headed renders
# differ.
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


_POPUP_REQUEST_SCRIPT = f"""
(() => {{
  const KEY = "{_POPUP_REQUEST_KEY}";
  const OPENED = "{_POPUP_OPENED_KEY}";
  if (Object.prototype.hasOwnProperty.call(window, KEY)) return;
  const native = window.open;
  if (typeof native !== "function") return;
  window[KEY] = null;
  window[OPENED] = false;
  // Captured before chrome.js shadows ``top`` (frame-bust neutralization), so
  // this is the *real* top document even from inside the shell's site iframe.
  // Publishing the record there lets the Python side read one frame instead of
  // interrogating every ad iframe on the page.
  const realTop = window.top;
  const parse = (features) => {{
    if (typeof features !== "string") return null;
    const sizes = {{}};
    for (const part of features.split(",")) {{
      const eq = part.indexOf("=");
      if (eq < 0) continue;
      const value = Number(part.slice(eq + 1).trim());
      if (Number.isFinite(value)) sizes[part.slice(0, eq).trim().toLowerCase()] = value;
    }}
    const width = sizes.width !== undefined ? sizes.width : sizes.innerwidth;
    const height = sizes.height !== undefined ? sizes.height : sizes.innerheight;
    if (!(width > 0) || !(height > 0)) return null;
    return {{ width: Math.round(width), height: Math.round(height) }};
  }};
  window.open = function (...args) {{
    window[OPENED] = true;
    try {{
      if (realTop && realTop !== window) realTop[OPENED] = true;
    }} catch (e) {{}}
    const requested = parse(args[2]);
    if (requested) {{
      window[KEY] = requested;
      // Best effort: a genuinely cross-origin opener frame cannot write to the
      // top document, and that is fine — the Python side falls back to a
      // bounded per-frame scan.
      try {{
        if (realTop && realTop !== window) realTop[KEY] = requested;
      }} catch (e) {{}}
    }}
    return native.apply(this, args);
  }};
}})();
"""


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
