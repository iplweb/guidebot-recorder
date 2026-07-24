"""``recorder.render.popup_detect``: the window.open geometry and "opened" lookups.

Split out of the original ``test_render.py``.
"""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.recorder import render as render_module
from guidebot_recorder.recorder.render import (
    _POPUP_REQUEST_SCRIPT,
    _parse_window_request,
    _popup_window_opened,
    _popup_window_request,
)

from ._render_helpers import FFMPEG

pytestmark = FFMPEG


# --- popup window geometry ---------------------------------------------------
# The context's viewport (and therefore record_video_size) is shared by every
# page, so a popup records onto a main-viewport-sized canvas. The site's own
# window.open features are the deterministic statement of the real window size.


@pytest.mark.parametrize(
    "requested, expected",
    [
        ({"width": 640, "height": 480}, (640, 480)),
        ({"width": 640.4, "height": 480.6}, (640, 480)),
        (None, None),
        ({"width": 0, "height": 480}, None),
        ({"width": -640, "height": 480}, None),
        ({"width": "640", "height": 480}, None),
        ({"width": 640}, None),
    ],
)
def test_parse_window_request(requested, expected):
    assert _parse_window_request(requested) == expected


async def test_popup_window_request_reads_window_open_features(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.set_content(
            "<button onclick=\"window.open('about:blank','p','width=420,height=300')\">go</button>"
        )

        # Nothing opened yet: no geometry, so the compositor must not crop.
        assert await _popup_window_request(page) is None

        async with context.expect_page():
            await page.click("button")

        assert await _popup_window_request(page) == (420, 300)
        await context.close()
        await browser.close()


async def test_popup_window_request_none_without_size_features(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        # A featureless window.open states no size; degrade to today's no-crop path.
        await page.set_content("<button onclick=\"window.open('about:blank','p')\">go</button>")

        async with context.expect_page():
            await page.click("button")

        assert await _popup_window_request(page) is None
        await context.close()
        await browser.close()


async def test_popup_window_request_finds_the_opener_iframe(tmp_path):
    """The click that opens the popup happens inside the shell's site iframe."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.set_content("<iframe id='site' srcdoc=\"<button>go</button>\"></iframe>")
        frame = page.frame_locator("#site")
        await frame.locator("button").evaluate(
            "el => el.onclick = () => window.open('about:blank','p','width=500,height=360')"
        )

        async with context.expect_page():
            await frame.locator("button").click()

        assert await _popup_window_request(page) == (500, 360)
        await context.close()
        await browser.close()


# --- popup window geometry: the lookup must never hang the render ------------
# An ad-heavy page carries iframes whose document never commits an execution
# context. ``Frame.evaluate`` has no timeout, so evaluating on one blocks
# forever — which is exactly how a real render deadlocked right after a popup
# opened. The lookup is therefore concurrent and bounded: one dead frame can no
# longer hide another frame's answer, and can never stall the render.


class _StubFrame:
    """A frame that answers the window.open probe immediately."""

    url = "https://example.test/"

    def __init__(self, requested):
        self._requested = requested

    async def evaluate(self, expression, *args):
        return self._requested


class _HangingFrame:
    """A frame whose execution context never materialises."""

    url = ""

    def __init__(self):
        self.entered = asyncio.Event()

    async def evaluate(self, expression, *args):
        self.entered.set()
        await asyncio.Event().wait()  # never resolves, exactly like the real one


class _RaisingFrame:
    """A frame whose probe blows up rather than answering."""

    url = "https://broken.test/"

    async def evaluate(self, expression, *args):
        raise RuntimeError("execution context destroyed")


class _StubOpener:
    def __init__(self, frames, main_frame=None):
        self.main_frame = main_frame if main_frame is not None else _StubFrame(None)
        self.frames = [self.main_frame, *frames]


async def test_popup_window_request_returns_as_soon_as_one_frame_answers():
    """A silent frame must not *delay* an answer another frame already gave.

    The budget is the ceiling for "nobody ever answers", not the price of every
    popup. Waiting for all probes made a single dead ad iframe cost the full
    ``_POPUP_REQUEST_LOOKUP_TIMEOUT`` on every popup open — a visible, dead
    pause in the rendered film. Deliberately *not* monkeypatching the timeout:
    the point of this test is the real-world duration.
    """
    hanging = _HangingFrame()
    opener = _StubOpener([_StubFrame({"width": 420, "height": 300}), hanging])

    started = time.monotonic()
    assert await _popup_window_request(opener) == (420, 300)
    elapsed = time.monotonic() - started

    assert hanging.entered.is_set(), "test did not exercise the hanging frame"
    assert elapsed < 0.5, (
        f"lookup waited for the silent frame: took {elapsed:.2f}s "
        f"(budget is {render_module.popup_detect._POPUP_REQUEST_LOOKUP_TIMEOUT:g}s)"
    )


async def test_popup_window_request_survives_a_frame_that_raises(monkeypatch):
    """One exploding probe must not abort the scan — other frames may answer."""
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener(
        [_StubFrame({"width": 420, "height": 300}), _RaisingFrame()],
        main_frame=_RaisingFrame(),
    )

    assert await _popup_window_request(opener) == (420, 300)


async def test_popup_window_request_takes_the_top_document_when_several_answer():
    """Several answers: the priority order decides, and it is deterministic."""
    opener = _StubOpener(
        [_StubFrame({"width": 800, "height": 600}), _StubFrame({"width": 640, "height": 480})],
        main_frame=_StubFrame({"width": 420, "height": 300}),
    )

    assert await _popup_window_request(opener) == (420, 300)


async def test_popup_window_request_answers_despite_a_frame_that_never_answers(monkeypatch):
    """A dead ad iframe must neither hang the lookup nor hide the real answer."""
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    hanging = _HangingFrame()
    # Reverse order puts the hanging frame ahead of the one holding the answer,
    # so a sequential scan would never reach the answer at all.
    opener = _StubOpener([_StubFrame({"width": 420, "height": 300}), hanging])

    started = time.monotonic()
    assert await _popup_window_request(opener) == (420, 300)
    elapsed = time.monotonic() - started

    assert hanging.entered.is_set(), "test did not exercise the hanging frame"
    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"


async def test_popup_window_request_gives_up_when_only_dead_frames_remain(monkeypatch, capsys):
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    started = time.monotonic()
    assert await _popup_window_request(opener) is None
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_popup_window_request_abandons_no_orphan_task(monkeypatch):
    """An abandoned probe must not leave an un-awaited exception behind."""
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    assert await _popup_window_request(opener) is None

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


# --- popup "opened" flag: the lookup must never hang the render either -------
# Mirrors the geometry lookup's hang-resistance tests above using the same
# stub frames — ``_scan_frames_for_window_opened`` is bounded exactly like
# ``_scan_frames_for_window_request``, and ``_popup_window_opened`` now carries
# the same outer hard timeout as ``_popup_window_request``. Unlike the
# geometry lookup, giving up here must still answer ``True`` ("assume a
# popup"), the fail-safe direction that never mounts an address bar on
# uncertainty.


async def test_popup_window_opened_answers_despite_a_frame_that_never_answers(monkeypatch):
    """A dead ad iframe must neither hang the lookup nor hide the real answer."""
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    hanging = _HangingFrame()
    # Reverse order puts the hanging frame ahead of the one holding the answer,
    # so a sequential scan would never reach the answer at all.
    opener = _StubOpener([_StubFrame(True), hanging])

    started = time.monotonic()
    assert await _popup_window_opened(opener) is True
    elapsed = time.monotonic() - started

    assert hanging.entered.is_set(), "test did not exercise the hanging frame"
    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"


async def test_popup_window_opened_gives_up_when_only_dead_frames_remain(monkeypatch, capsys):
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    started = time.monotonic()
    # Unlike the geometry lookup (which gives up to "no crop"), giving up here
    # must still assume a popup, so no address bar gets mounted on uncertainty.
    assert await _popup_window_opened(opener) is True
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"lookup was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_popup_window_opened_abandons_no_orphan_task(monkeypatch):
    """An abandoned probe must not leave an un-awaited exception behind."""
    monkeypatch.setattr(render_module.popup_detect, "_POPUP_REQUEST_LOOKUP_TIMEOUT", 0.3)
    opener = _StubOpener([_HangingFrame()], main_frame=_HangingFrame())

    assert await _popup_window_opened(opener) is True

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


async def test_popup_window_request_prefers_the_top_documents_record():
    """The init script republishes on the top document; it wins over stale frames."""
    opener = _StubOpener(
        [_StubFrame({"width": 800, "height": 600})],
        main_frame=_StubFrame({"width": 420, "height": 300}),
    )
    assert await _popup_window_request(opener) == (420, 300)


async def test_popup_window_request_warns_when_nothing_recorded(capsys):
    assert await _popup_window_request(_StubOpener([_StubFrame(None)])) is None
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_window_open_call_is_recorded_even_without_size_features():
    # A featureless `window.open` must be distinguishable from no call at all:
    # only the latter is a `target=_blank` tab.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_init_script(script=render_module._POPUP_REQUEST_SCRIPT)
        page = await context.new_page()
        await page.goto("data:text/html,<p>opener</p>")

        assert await render_module._popup_window_opened(page) is False

        await page.evaluate("window.open('about:blank', 'named')")
        assert await render_module._popup_window_opened(page) is True
        assert await render_module._popup_window_request(page) is None

        await browser.close()


def _start_static_http_server(body: bytes) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Serve ``body`` at ``/`` on an ephemeral 127.0.0.1 port; return (server, thread, origin)."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence the test server
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


async def test_popup_window_opened_is_true_from_a_cross_origin_opener_iframe():
    """The click that opens the popup happens inside a genuinely cross-origin
    site iframe — unlike ``test_popup_window_request_finds_the_opener_iframe``,
    which uses ``srcdoc`` and is therefore same-origin with the opener's top
    document. Here the iframe's ``realTop[OPENED] = true`` mirror write throws
    a real cross-origin ``SecurityError`` (swallowed by the init script's own
    ``catch``), so only a per-frame scan — not a top-frame-only read — can see
    that ``window.open`` was called at all.
    """

    inner_body = (
        b"<!doctype html><body>"
        b"<button id='go' onclick=\"window.open('about:blank','p','width=500,height=400')\">"
        b"go</button></body>"
    )
    inner_server, inner_thread, inner_origin = _start_static_http_server(inner_body)
    outer_body = (
        f"<!doctype html><body><iframe id='site' src='{inner_origin}/'></iframe></body>"
    ).encode()
    outer_server, outer_thread, outer_origin = _start_static_http_server(outer_body)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 640, "height": 480})
            await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
            page = await context.new_page()
            await page.goto(outer_origin)
            frame = page.frame_locator("#site")

            # Nothing opened yet, and the two documents are on distinct origins.
            assert await render_module._popup_window_opened(page) is False

            async with context.expect_page():
                await frame.locator("#go").click()

            assert await render_module._popup_window_opened(page) is True
            # The sibling geometry lookup already handled this correctly; must
            # keep doing so unchanged.
            assert await _popup_window_request(page) == (500, 400)

            await context.close()
            await browser.close()
    finally:
        inner_server.shutdown()
        inner_thread.join()
        outer_server.shutdown()
        outer_thread.join()
