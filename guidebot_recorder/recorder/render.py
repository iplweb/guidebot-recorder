"""The `render` phase — deterministic replay + film assembly (§8/§9).

Phase 0: pre-synthesize every configured narration track into the cache.
Render: 0×LLM, fresh browser, single pass; narration drives the pace.
Assembly: Playwright video + language audio beds (ffmpeg), approximate sync (K2).

Resolved actions are read from the separate ``*.compiled.yaml`` sidecar.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    Frame,
    Page,
    Video,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from tqdm import tqdm

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.desktop import DesktopOverlay, resolve_icon
from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import (
    COMPILER_VERSION,
    CachedAction,
    Fingerprint,
    PendingAction,
)
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.config import (
    ChromeConfig,
    SoundConfig,
    TtsConfig,
    Viewport,
    config_hash,
)
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import (
    pause_for_inspection,
    redact_exception,
    scenario_sensitive_values,
)
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError
from guidebot_recorder.recorder.session import ensure_session
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    TargetAbsent,
    compiled_from,
    heuristic_expect,
    resolve_step_target,
    step_instruction,
)
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.selects import SelectsNotReadyError, install_selects
from guidebot_recorder.slide import SlideOverlay
from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    TtsProvider,
    cache_key,
)
from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import (
    FadeSpec,
    MuxAudioTrack,
    compose_popup_video,
    detect_content_crop,
    mux_audio_tracks,
    probe_duration,
)
from guidebot_recorder.video.sfx import build_sfx_bed, mix_sfx_into_bed
from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    apply_time_edits,
    assert_recording_fps,
    frames_to_seconds,
    probe_frame_count,
    seconds_to_frames,
)

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1
_VIDEO_POSTROLL_SECONDS = 0.1
_TTS_CONCURRENCY = 8
#: how often a pending gate is re-resolved while its wait window is still open
_PENDING_POLL_SECONDS = 0.25
# Each worker can own a full ffmpeg process. Keep the pool below both the host's
# CPU count and a conservative process ceiling instead of scaling with languages.
_AUDIO_BED_CONCURRENCY = max(1, min(4, os.cpu_count() or 1))


class RenderError(RuntimeError):
    """A step needs (re-)compile: missing action or mismatched identity."""


class _OptionalAbsent(Exception):
    """The element an *optional* step or branch gate stands for is simply not there.

    Deliberately narrow, and deliberately not a :class:`RenderError`: it is raised
    only from the four signals the design admits as "absent" (a timed-out cached
    ``waitFor``, a ``no_action``/``no_handle`` verdict, an elapsed poll window, a
    failed ``reuse_is_valid``). Everything else — an ambiguous description, a click
    that fails on a resolved target, a navigation error — keeps propagating and
    fails the render, so ``optional`` cannot decay into ``except Exception: pass``.
    """


#: A slide card's on-screen content, as consumed by ``SlideOverlay.show``/``.ensure``.
Card = dict[str, str | None]


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
    context is created (see ``render.py`` around the ``browser.new_context`` call),
    so it already is the canvas measured in recording pixels — the same unit
    ``popup_crop`` is in. No CSS-to-recording-pixel conversion belongs here.
    """

    if popup_crop is None:
        return True
    width, height, x, y = popup_crop
    return (x, y) == (0, 0) and (width, height) == (viewport.width, viewport.height)


@dataclass(slots=True)
class _TtsWork:
    text: str
    config: TtsConfig
    destinations: list[tuple[str, int]]


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


#: Desktop-opener beats, in ms. The window growth (`_DESKTOP_OPEN_MS`) is the one
#: the eye actually reads; the rest are short settles that keep the double-click
#: legible without dragging the opener out.
_DESKTOP_SETTLE_MS = 260
_DESKTOP_DOUBLE_CLICK_GAP_MS = 130
_DESKTOP_PRE_OPEN_MS = 220
_DESKTOP_OPEN_MS = 760


async def _play_desktop_opener(
    desktop: DesktopOverlay,
    overlay: Overlay,
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
    overlay: Overlay,
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
    overlay: Overlay,
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
    overlay: Overlay,
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


async def _hand_cursor_to_popup(main_page: Page, popup: _PopupSession, overlay: Overlay) -> None:
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


def _narration(step: Step) -> str | None:
    return step.narration()


def _stamp_frame(anchor: float, *, not_before: int = 0) -> int:
    """Stamp "now" as a recording frame index, never earlier than *not_before*.

    Every audio placement is a wall-clock reading quantised onto the 25fps grid,
    and every such reading is later mapped through :meth:`Timeline.to_virtual`,
    which shifts a stamp past a freeze only when the freeze sits STRICTLY before
    it — a stamp exactly AT its own freeze point must stay put, because that is
    where the narration the freeze exists for begins.

    That rule is right, but it makes the grid unforgiving: a freeze recorded at
    frame ``F`` and a later event whose reading also rounds to ``F`` (the work
    in between took less than 40ms — a couple of CDP round-trips easily fits)
    are indistinguishable, so the later event maps to the START of the hold and
    fires up to a whole narration early. Nothing bounds that gap: ``settle``
    separates the step's own start from ``F``, not ``F`` from what follows it.

    So stamps are made monotonic against the freezes already emitted: once a
    freeze exists at ``F``, everything stamped afterwards is at least ``F + 1``
    and therefore lands after the hold. The cost is at most one frame (40ms) of
    placement error on an event that genuinely happened within the same frame,
    which is below the resolution the axis can represent at all.
    """
    return max(seconds_to_frames(time.monotonic() - anchor), not_before)


async def _pace_narration(
    segments: list[Segment],
    *,
    anchor: float,
    hold_frame: bool,
    settle: float,
    edits: list[TimeEdit],
    not_before: int = 0,
) -> int | None:
    """Pace one shared visual step by its longest configured narration.

    With ``hold_frame`` the wall clock only pays ``settle`` seconds — enough for
    entry animations triggered by this step to finish — and the rest of the
    voice-over becomes a held frame inserted in post. The settle comes *out of*
    the narration, not on top of it, so the finished film keeps the exact pacing
    it had when the renderer slept through the whole thing.

    Returns the recording frame the freeze was stamped at, or ``None`` when no
    freeze was recorded. *not_before* is the earliest frame this freeze may be
    stamped at — see :func:`_stamp_frame`; freezes are stamped through the same
    monotonic rule as everything else so a later freeze can never precede an
    earlier one on the recording axis.
    """

    if not segments:
        return None
    duration = max(segment.duration for segment in segments)

    if not hold_frame:
        await asyncio.sleep(duration)
        return None

    real = min(settle, duration)
    await asyncio.sleep(real)

    remaining = duration - real
    if remaining <= 0:
        return None
    at = _stamp_frame(anchor, not_before=not_before)
    edits.append(TimeEdit(at=at, kind="freeze", frames=seconds_to_frames(remaining)))
    return at


def _build_timeline(edits: Iterable[TimeEdit], *, source_frames: int) -> Timeline:
    """Coalesce collected edits onto the frame grid, then validate them.

    ``Timeline`` is deliberately strict — it rejects two edits on one frame, and
    anything at or past the end of the recording — because those are nonsense as
    a *model*. They are not nonsense as *observations*: a freeze recorded near
    the end can round past the 0.1s postroll and land at or beyond the last
    frame, and the clamp that pulls it back can then collide with a freeze
    already sitting there. Either would otherwise blow up after the entire
    recording is finished, losing the render.

    (``_stamp_frame`` now keeps freezes at least a frame apart as they are
    emitted, so two *unclamped* freezes can no longer share a frame. The merge
    still has to exist for the clamped case, and is kept general rather than
    special-cased to it.)

    So the collected list is reconciled here, where the observations are:

    * an ``at`` at or beyond the end clamps to the last real frame — there is no
      later frame to hold, and the film must still gain those frames;
    * freezes sharing a frame merge by SUMMING their lengths, because two steps
      that both want the picture held at frame N mean the film holds frame N for
      the total of both. The film comes out exactly as long as the narration
      asked for, which is the invariant that matters.
    """
    merged: dict[int, TimeEdit] = {}
    passthrough: list[TimeEdit] = []
    for edit in edits:
        if edit.kind != "freeze":
            passthrough.append(edit)
            continue
        at = min(edit.at, source_frames - 1)
        previous = merged.get(at)
        frames = edit.frames + (previous.frames if previous else 0)
        merged[at] = TimeEdit(at=at, kind="freeze", frames=frames)
    return Timeline.build([*merged.values(), *passthrough], source_frames=source_frames)


def _apply_timeline_edits(source: Path, timeline: Timeline, dest: Path) -> None:
    """Apply *timeline* to *source*, then verify the result against the model.

    Everything downstream trusts ``Timeline.virtual_duration``: the audio beds
    are built to that length and ``mux_audio_tracks`` is handed the same number
    as its ``video_duration``, so its duration guard compares the model against
    itself and can never catch a model/file disagreement. This is the one place
    the model meets the file, so the check is exact — both sides are integer
    frame counts, and a difference of even one frame means the filtergraph did
    something other than what was modelled.
    """
    apply_time_edits(source, timeline, dest)
    produced = probe_frame_count(dest)
    if produced != timeline.virtual_frames:
        raise RenderError(
            f"time-edit stage produced {produced} frames but the timeline models "
            f"{timeline.virtual_frames} — audio would be written at the wrong length"
        )


async def _presynthesize_narration(
    steps: Sequence[Step],
    configs: list[TtsConfig],
    cache: TtsCache,
    provider: TtsProvider,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, dict[int, Segment]]:
    """Synthesize unique cache entries concurrently and map them to every step.

    Repeated narration can resolve to the same on-disk cache path. Deduplicating
    by the canonical cache key before scheduling prevents concurrent writers to
    that path and avoids duplicate provider calls on a cold cache.
    """

    segments: dict[str, dict[int, Segment]] = {tts.lang: {} for tts in configs}
    by_key: dict[str, _TtsWork] = {}
    for index, step in enumerate(steps):
        canonical_text = _narration(step)
        if canonical_text is None:
            continue
        for track_index, tts in enumerate(configs):
            text = canonical_text if track_index == 0 else step.translations[tts.lang]
            key = cache_key(
                text,
                tts,
                provider.adapter_version,
                CACHE_SCHEMA_VERSION,
            )
            work = by_key.get(key)
            if work is None:
                work = _TtsWork(text=text, config=tts, destinations=[])
                by_key[key] = work
            work.destinations.append((tts.lang, index))

    semaphore = asyncio.Semaphore(_TTS_CONCURRENCY)

    async def synthesize(work: _TtsWork) -> None:
        async with semaphore:
            segment = await cache.get_or_synth(work.text, work.config, provider)
        for language, index in work.destinations:
            segments[language][index] = segment
        if on_progress is not None:
            on_progress(len(work.destinations))

    results = await asyncio.gather(
        *(synthesize(work) for work in by_key.values()),
        return_exceptions=True,
    )
    # Wait for every started cache writer before propagating an error; otherwise
    # a sibling task could keep writing after Phase 0 has already returned.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return segments


async def _mux_tracks_for_timeline(
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    *,
    sfx_bed: Path | None = None,
) -> list[MuxAudioTrack]:
    """Build one full-length bed per language in deterministic stream order.

    When *sfx_bed* is set, narration is rendered to a temp name first, then the
    shared SFX bed is mixed into the final `bed-<lang>.wav` so ``bed-*.wav`` keeps
    naming ``_publish_render_artifacts`` relies on.
    """

    for tts in configs:
        for placement in placed_by_language[tts.lang]:
            if placement.offset + placement.segment.duration > total:
                raise RenderError(
                    f"narracja {tts.lang} wykracza poza nagranie wideo — render przerwany"
                )

    semaphore = asyncio.Semaphore(_AUDIO_BED_CONCURRENCY)

    def build_track(index: int, tts: TtsConfig) -> MuxAudioTrack:
        bed = work / f"bed-{tts.mp4_language()}.wav"
        if sfx_bed is not None:
            narr = work / f"narr-{tts.mp4_language()}.wav"
            build_audio_bed(placed_by_language[tts.lang], total, narr)
            mix_sfx_into_bed(narr, sfx_bed, bed, total)  # bed = narration + SFX
        else:
            build_audio_bed(placed_by_language[tts.lang], total, bed)
        return MuxAudioTrack(
            path=bed,
            language=tts.mp4_language(),
            title=tts.title or tts.lang,
            default=index == 0,
        )

    async def build_bounded(index: int, tts: TtsConfig) -> MuxAudioTrack:
        async with semaphore:
            worker = asyncio.create_task(asyncio.to_thread(build_track, index, tts))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an asyncio wrapper cannot stop a running thread (or
                # its ffmpeg child). Keep the staging directory alive until that
                # worker has actually returned, with caller cancellation primary.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                if not worker.cancelled():
                    try:
                        worker.result()
                    except BaseException:
                        pass
                raise

    tasks = [asyncio.create_task(build_bounded(index, tts)) for index, tts in enumerate(configs)]
    gathered = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(gathered)
    except asyncio.CancelledError:
        # Do not start queued ffmpeg work after cancellation, but let workers
        # already inside to_thread finish before TemporaryDirectory can unwind.
        for task in tasks:
            task.cancel()
        while not gathered.done():
            try:
                await asyncio.shield(gathered)
            except asyncio.CancelledError:
                continue
        if not gathered.cancelled():
            gathered.result()
        raise
    tracks: list[MuxAudioTrack] = []
    # gather preserves config order. It also waits for all ffmpeg workers before
    # an error leaves the staging directory, avoiding writes into deleted paths.
    for result in results:
        if isinstance(result, BaseException):
            raise result
        tracks.append(result)
    return tracks


def _publish_render_artifacts(
    staged_mp4: Path,
    tracks: list[MuxAudioTrack],
    work: Path,
    out_mp4: Path,
) -> None:
    """Commit the new master and complete bed set, rolling back publish errors."""

    backup = Path(tempfile.mkdtemp(prefix=".audio-beds-backup-", dir=work))
    published: list[Path] = []
    try:
        for current in list(work.glob("bed-*.wav")):
            os.replace(current, backup / current.name)
        for track in tracks:
            destination = work / track.path.name
            os.replace(track.path, destination)
            published.append(destination)
        # The master is the commit point: until this atomic replace succeeds, the
        # previous MP4 remains in place and any bed publication error is rolled back.
        os.replace(staged_mp4, out_mp4)
    except BaseException:
        for destination in published:
            destination.unlink(missing_ok=True)
        for previous in backup.glob("bed-*.wav"):
            os.replace(previous, work / previous.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


async def _assemble_audio_tracks(
    video: Path,
    configs: list[TtsConfig],
    placed_by_language: dict[str, list[Placed]],
    total: float,
    work: Path,
    out_mp4: Path,
    *,
    preencoded: bool = False,
    sound: SoundConfig | None = None,
    sfx_offsets: list[tuple[str, float]] | None = None,
    fade: FadeSpec | None = None,
) -> None:
    """Stage a complete bed set, mux atomically, then publish durable WAVs.

    When *sound* is enabled and *sfx_offsets* is non-empty, the shared SFX bed is
    built ONCE in staging (from the packaged click/key assets) and mixed into every
    language's narration bed via `_mux_tracks_for_timeline`.
    """

    with tempfile.TemporaryDirectory(prefix=".audio-beds-", dir=work) as staging:
        staged_mp4 = Path(staging) / f"{out_mp4.stem}.mp4"
        sfx_bed = None
        if sound is not None and sound.enabled and sfx_offsets:
            sfx_bed = Path(staging) / "sfx-bed.wav"
            sfx_pkg = files("guidebot_recorder.sfx")
            with (
                as_file(sfx_pkg.joinpath("click.wav")) as cp,
                as_file(sfx_pkg.joinpath("key.wav")) as kp,
            ):
                build_sfx_bed(
                    sfx_offsets,
                    total,
                    sfx_bed,
                    click_path=Path(cp),
                    key_path=Path(kp),
                    gain_db=sound.volume,
                )
        tracks = await _mux_tracks_for_timeline(
            configs,
            placed_by_language,
            total,
            Path(staging),
            sfx_bed=sfx_bed,
        )
        mux_audio_tracks(
            video,
            tracks,
            staged_mp4,
            preencoded=preencoded,
            video_duration=total,
            fade=fade,
        )
        _publish_render_artifacts(staged_mp4, tracks, work, out_mp4)


def _resolve_url(scenario: Scenario, url: str) -> str:
    base = scenario.config.base_url
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


def _compiled_from(step: Step) -> str:
    """What ``compile`` froze this step's fingerprint against.

    A thin alias, deliberately not a second implementation: render's copy of
    this rule used to be a verbatim duplicate of the compiler's, so extending
    one side (with the per-step ``select.mode``) would have made every sidecar
    look stale to the other.
    """

    try:
        return compiled_from(step)
    except ValueError as exc:
        raise ValueError(f"krok {step.command_kind()} nie wymaga cachedAction") from exc


def _compiled_action_is_current(
    step: Step, action: CompiledAction | None, scenario_hash: str
) -> bool:
    """Check source/config fingerprints before replaying frozen behavior."""

    if not step.requires_target():
        return action is None
    if action is None:
        return False
    kind = step.command_kind()
    expected_state = step.wait.state if isinstance(step.wait, WaitUntil) else None
    if isinstance(action, PendingAction):
        # Nothing was frozen yet, so there is no action/expect to cross-check —
        # only that the placeholder still stands for *this* step and config.
        fingerprint = action.fingerprint
        return (
            fingerprint.compiler_version == COMPILER_VERSION
            and fingerprint.command_kind == kind
            and fingerprint.compiled_from == _compiled_from(step)
            and fingerprint.config_hash == scenario_hash
            and fingerprint.state == expected_state
        )
    expected_action = {
        "click": "click",
        "hover": "hover",
        "enterText": "type",
        "select": "select",
        "highlight": "highlight",
        "wait": "waitFor",
    }.get(kind)
    if expected_action is not None and action.action != expected_action:
        return False
    fingerprint = action.fingerprint
    if not (
        fingerprint.compiler_version == COMPILER_VERSION
        and fingerprint.command_kind == kind
        and fingerprint.compiled_from == _compiled_from(step)
        and fingerprint.config_hash == scenario_hash
        and fingerprint.state == expected_state
        and fingerprint.expect == action.expect
    ):
        return False
    return not (
        kind == "teach"
        and action.action == "type"
        and (action.input_text is None or action.input_text not in step.teach)
    )


async def _resolve_pending_target(
    root: Page | Frame,
    step: Step,
    kind: str,
    reasoner: Reasoner,
) -> ResolvedTarget:
    """Resolve a :class:`PendingAction` against the live page, polling while it may still appear.

    A gate (`wait: {until: ...}`) gets the whole configured wait window, retried on
    an interval: the canonical gating element — a cookie banner — is injected after
    a delay, and a single snapshot would report a spurious absence and silently
    delete the branch from the video. Any other optional step has no wait window,
    so it is resolved exactly once.

    Raises :class:`_OptionalAbsent` when the window closes on an "absent" verdict;
    every other resolver failure propagates out of ``resolve_step_target``.
    """

    window = step.wait.timeout if isinstance(step.wait, WaitUntil) else 0.0
    deadline = time.monotonic() + window
    while True:
        result = await resolve_step_target(root, step, kind, reasoner)
        if isinstance(result, ResolvedTarget):
            return result
        assert isinstance(result, TargetAbsent)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _OptionalAbsent(f"{step_instruction(step)!r}: {result.reason} ({result.message})")
        await asyncio.sleep(min(_PENDING_POLL_SECONDS, remaining))


def _freeze_resolved(
    step: Step,
    kind: str,
    resolved: ResolvedTarget,
    expect: str,
    scenario_hash: str,
) -> CachedAction:
    """Build the ``CachedAction`` that replaces a pending entry in the sidecar.

    ``opens_popup`` stays false by construction: a click resolved at render time
    carries no popup observation from compile, and the render popup contract is
    what fails loudly if one opens anyway (a documented limitation of branches).
    """

    return CachedAction(
        action=resolved.action,
        target=resolved.target,
        identity=resolved.identity,
        expect=expect,
        state=resolved.state,
        input_text=resolved.input_text,
        fingerprint=Fingerprint(
            command_kind=kind,
            compiled_from=_compiled_from(step),
            expect=expect,
            config_hash=scenario_hash,
            state=resolved.state,
        ),
    )


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
    path = Path(path)
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    scenario = load_scenario(path, env)
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cfg = scenario.config
    # Caller-side overrides (the CLI flags). ``None`` means "use whatever the
    # scenario configured" — the scenario is loaded here, so an override applied
    # to a Config built by the caller would be discarded.
    if hold_frame is not None:
        cfg.hold_frame_for_narration = hold_frame
    if hold_frame_settle is not None:
        cfg.hold_frame_settle = hold_frame_settle
    audio_configs = [cfg.tts, *cfg.audio_tracks]
    providers = {tts.provider for tts in audio_configs}
    if len(providers) != 1:
        raise RenderError(
            "jeden render obsługuje obecnie jeden provider TTS; "
            f"skonfigurowano: {', '.join(sorted(providers))}"
        )

    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise RenderError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if compiled.source != path.name:
        raise RenderError(
            f"compiled pochodzi z innego scenariusza ({compiled.source}) — uruchom `compile`"
        )
    # Flat indexing: a `when:` block contributes its synthetic gate step followed by
    # its children, so `actions`, narration segments and every `krok {index}` message
    # index the same linear execution order.
    flat = scenario.flat_steps()
    flat_steps = [entry.step for entry in flat]
    if len(compiled.actions) != len(flat):
        raise RenderError("compiled niezgodny z liczbą kroków — uruchom `compile`")
    if compiled.compiler_version != COMPILER_VERSION or any(
        action is not None and action.fingerprint.compiler_version != COMPILER_VERSION
        for action in compiled.actions
    ):
        raise RenderError("compiled ma starszą wersję — uruchom `compile`")
    scenario_hash = config_hash(cfg)

    def step_message(
        entry: FlatStep, entry_index: int, message: str, *, warning: bool = False
    ) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=entry_index,
            total=len(flat),
            location=entry.location,
            source=scenario.source,
            message=message,
            warning=warning,
            sensitive=sensitive_values,
        )

    for index, (entry, action) in enumerate(zip(flat, compiled.actions, strict=True)):
        if not _compiled_action_is_current(entry.step, action, scenario_hash):
            raise RenderError(
                step_message(entry, index, "compiled jest nieaktualny — uruchom `compile`")
            )
        if isinstance(action, PendingAction) and entry.branch is None and not entry.step.optional:
            # A pending entry is only ever written for a branch (gate + children)
            # or an `optional: true` step; anywhere else the sidecar is corrupt.
            raise RenderError(
                step_message(
                    entry, index, "wpis oczekujący na kroku obowiązkowym — uruchom `compile`"
                )
            )

    # Desktop icons are resolved here, before recording: an unknown built-in or a
    # missing file is an authoring error and must fail loud up front, not after
    # minutes of render. Relative icon paths resolve against the scenario file's
    # directory. Keyed by flat-step index for the render loop to read back.
    desktop_payloads: dict[int, dict[str, str]] = {}
    for index, step in enumerate(flat_steps):
        if step.desktop is not None:
            desktop_payloads[index] = {
                "color": cfg.desktop.color,
                "label": step.desktop.label,
                **resolve_icon(step.desktop, base_dir=path.parent),
            }

    # --- Faza 0: pre-synteza całej narracji (fail-loud przed nagrywaniem) ---
    cache = TtsCache(cache_dir)
    narration_count = sum(_narration(step) is not None for step in flat_steps)
    presynth = tqdm(
        total=narration_count * len(audio_configs),
        desc="tts",
        unit="segment",
        disable=not verbose,
    )
    try:
        segments = await _presynthesize_narration(
            flat_steps,
            audio_configs,
            cache,
            tts_provider,
            on_progress=presynth.update,
        )
    finally:
        presynth.close()

    # --- Render z nagrywaniem wideo (viewport z config — patrz compile) ---
    work = out_mp4.parent / ".guidebot_video" / out_mp4.stem
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
    # `card_active`/`active_card` track whether a slide card currently owns the
    # screen (painted either by a `slide` step or the auto-intro below). When no
    # card is ever painted (no `slide` steps, `intro.enabled=False`), these stay
    # False/None for the whole render and every helper below is a pure pass-
    # through to today's `_ensure_visuals` — i.e. byte-identical back-compat.
    card_active = False
    active_card: Card | None = None

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
        assert active_card is not None  # guaranteed by the card_active invariant
        await slide.ensure(pg, active_card)
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
        active_card = {
            "title": cfg.title,
            "subtitle": cfg.intro.subtitle,
            "notes": cfg.intro.notes,
        }
        await slide.show(page, active_card)
        await overlay.hide(page)
        await _chrome_hide(page)
        card_active = True
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

    def note_skip(entry: FlatStep, entry_index: int, reason: str, *, gate: bool) -> None:
        """Odnotuj pominięty krok opcjonalny — banner z `plik:linia`."""

        what = "bramka" if gate else "krok opcjonalny"
        tqdm.write(step_message(entry, entry_index, f"{what} pominięty — {reason}", warning=True))

    def persist_resolved(entry_index: int, resolved_action: CachedAction) -> None:
        """Fold a render-time resolution back into the sidecar (full atomic rewrite)."""

        compiled.actions[entry_index] = resolved_action
        write_compiled(cpath, compiled)

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
                if card_active:
                    await _assert_card_alive(active_page)
                    await slide.hide(active_page)
                    card_active = False
                    active_card = None

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
                # state, so from here it is exactly the no-card path (card_active
                # stays False).
            elif kind == "slide":
                assert step.slide is not None  # guaranteed by command_kind()
                if card_active:
                    # Fail loud before repainting: a slide following a say whose
                    # card was destroyed mid-narration must NOT silently swap in a
                    # fresh card over the wrong page (mirrors the generic dismiss
                    # branch's token assert below).
                    await _assert_card_alive(active_page)
                    await slide.hide(active_page)
                    await overlay.show(active_page)
                    await _chrome_show(active_page)
                active_card = {
                    "title": step.slide.title,
                    "subtitle": step.slide.subtitle,
                    "notes": step.slide.notes,
                }
                await slide.show(active_page, active_card)
                await overlay.hide(active_page)
                await _chrome_hide(active_page)
                card_active = True
            elif kind == "say" and card_active:
                await _ensure_card(active_page)
            elif card_active:
                await _assert_card_alive(active_page)
                await slide.hide(active_page)
                await overlay.show(active_page)
                await _chrome_show(active_page)
                card_active = False
                active_card = None
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
            # it stays in `_render_step` (a synthetic gate step never narrates).
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
                    note_skip(entry, index, str(absent), gate=entry.is_gate)
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
                emitted = await _pace_narration(
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
            if card_active:
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
                opened = await _render_step(
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
                    on_resolved=persist_resolved,
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
                        await _prepare_main_after_popup_close(
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
                note_skip(entry, index, str(absent), gate=entry.is_gate)
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
        closed_at = probe_duration(main_webm) if popup_open_at_end else popup.closed_at
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
        _apply_timeline_edits(source_video, timeline, edited)
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

    await _assemble_audio_tracks(
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


async def _render_step(
    page: Page,
    recorder: Recorder,
    overlay: Overlay,
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
        # `_prepare_main_after_popup_close` with the saved cursor position. Do not
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


async def _prepare_popup(
    page: Page,
    overlay: Overlay,
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
