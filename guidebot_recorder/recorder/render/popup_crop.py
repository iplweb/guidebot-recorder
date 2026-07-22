"""Framing a popup: the three-level crop chain and its unit conversion.

A popup records onto the *main* window's canvas with filler around its real
window, so composition has to crop it back down. Three witnesses, most
deterministic first:

1. the ``window.open`` size features — read in
   :mod:`~guidebot_recorder.recorder.render.popup_detect`, while the opener lives;
2. the popup's own painted content box — measured inside the page, here;
3. ``cropdetect`` over the finished recording — a pixel heuristic, last resort.

The three are one story ("most trustworthy first") and stay in one module for that
reason. Levels 1 and 2 speak CSS pixels while the crop is applied to the
recording, and those are the same unit only when the compositor draws at scale 1;
:func:`_recording_scale` and :func:`_scale_rect` are that conversion, which is why
this module owns both the measurement and the arithmetic.

Two test seams are patched on *this* module: ``detect_content_crop`` (level 3),
name-imported below because this is the module whose globals the consumer reads,
and ``_POPUP_CONTENT_BOX_TIMEOUT``, defined and read here for the same reason.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.models.config import Viewport
from guidebot_recorder.video.mux import detect_content_crop

from .popup_session import _PopupSession
from .tasks import _discard_pending

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
_POPUP_CONTENT_BOX_SCRIPT = """
() => {
  const body = document.body;
  if (!body) return null;
  const vw = Math.round(window.innerWidth);
  const vh = Math.round(window.innerHeight);
  if (!(vw > 0) || !(vh > 0)) return null;
  // A popup that paints its own page background is full-bleed by construction:
  // the background covers the whole window, so the union of body's children is
  // only the *ink* on top of it and cropping to that would cut the popup's
  // background away. ``body``/``documentElement`` are skipped everywhere else in
  // this walk precisely because their box is the context viewport — but whether
  // they PAINT is a different question, and the honest answer here is "decline".
  // An unstyled document leaves both transparent (the white is the canvas, not a
  // background), so ordinary dialogs are unaffected.
  const paintsPage = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const bg = style.backgroundColor;
    if (bg && bg !== "transparent" && !/^rgba\\(0,\\s*0,\\s*0,\\s*0\\)$/.test(bg)) return true;
    return Boolean(style.backgroundImage) && style.backgroundImage !== "none";
  };
  if (paintsPage(document.documentElement) || paintsPage(body)) return null;
  const MAX_DEPTH = 12;
  const MAX_NODES = 1200;
  // The walk runs on the popup's main thread while the popup is on camera, so
  // it is bounded by wall clock as well as by node count: whatever the DOM
  // looks like, it cannot cost the recording more than this. Overrunning
  // abandons the measurement (partial unions are worse than none — they would
  // crop away real content), and level 3 decides instead.
  const BUDGET_MS = 20;
  const deadline = performance.now() + BUDGET_MS;
  let overran = false;
  const REPLACED = new Set([
    "IMG", "SVG", "CANVAS", "VIDEO", "IFRAME", "INPUT", "TEXTAREA",
    "SELECT", "BUTTON", "OBJECT", "EMBED", "HR",
  ]);
  let visited = 0;
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  const add = (rect) => {
    if (!rect) return;
    const l = Math.max(0, rect.left);
    const t = Math.max(0, rect.top);
    const r = Math.min(vw, rect.right);
    const b = Math.min(vh, rect.bottom);
    if (!(r > l) || !(b > t)) return;
    if (l < left) left = l;
    if (t < top) top = t;
    if (r > right) right = r;
    if (b > bottom) bottom = b;
  };
  const isOverlay = (el) => {
    for (const attr of el.attributes || []) {
      if (attr.name.startsWith("data-guidebot")) return true;
    }
    return false;
  };
  const paints = (el, style) => {
    if (REPLACED.has(el.tagName)) return true;
    const bg = style.backgroundColor;
    if (bg && bg !== "transparent" && !/^rgba\\(0,\\s*0,\\s*0,\\s*0\\)$/.test(bg)) return true;
    if (style.backgroundImage && style.backgroundImage !== "none") return true;
    if (style.boxShadow && style.boxShadow !== "none") return true;
    for (const side of ["Top", "Right", "Bottom", "Left"]) {
      if (parseFloat(style["border" + side + "Width"]) > 0) return true;
    }
    return false;
  };
  const walk = (el, depth) => {
    if (overran) return;
    if (visited++ > MAX_NODES || performance.now() > deadline) {
      overran = true;
      return;
    }
    if (isOverlay(el)) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return;
    if (Number(style.opacity) === 0) return;
    if (paints(el, style) || depth >= MAX_DEPTH) {
      add(el.getBoundingClientRect());
      return;
    }
    let descended = false;
    for (const node of el.childNodes) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        walk(node, depth + 1);
        descended = true;
      } else if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) {
        // Measure the glyphs, not the block box the text sits in: a <p> is as
        // wide as its container, its text usually is not.
        const range = document.createRange();
        range.selectNodeContents(node);
        add(range.getBoundingClientRect());
        descended = true;
      }
    }
    if (!descended) add(el.getBoundingClientRect());
  };
  for (const child of body.children) walk(child, 1);
  if (overran) return null;
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
  if (!(right > left) || !(bottom > top)) return null;
  return {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
    viewportWidth: vw,
    viewportHeight: vh,
  };
}
"""


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
    context is created (see ``_run``, around the ``browser.new_context`` call),
    so it already is the canvas measured in recording pixels — the same unit
    ``popup_crop`` is in. No CSS-to-recording-pixel conversion belongs here.
    """

    if popup_crop is None:
        return True
    width, height, x, y = popup_crop
    return (x, y) == (0, 0) and (width, height) == (viewport.width, viewport.height)
