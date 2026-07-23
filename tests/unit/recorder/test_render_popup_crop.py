"""``recorder.render.popup_crop``: content-box measurement and the crop fallback chain.

Split out of the original ``test_render.py``. The window.open geometry lookup
referenced in some comments lives in ``test_render_popup_detect.py``.
"""

import asyncio
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.recorder import render as render_module
from guidebot_recorder.recorder.render import (
    POPUP_BBOX_DEGENERATE_RATIO,
    _parse_content_box,
    _popup_content_box,
    _popup_fills_canvas,
    _resolve_popup_crop,
)

from ._render_helpers import FFMPEG

pytestmark = FFMPEG


# --- popup crop level 2: the content bounding box ----------------------------
# A featureless ``window.open(url, name)`` states no size, so the popup's own
# painted content is the next-best witness. ``body``/``documentElement`` are
# useless here (they are the *context* viewport), hence the union over body's
# children; a union that still fills the viewport is refused as degenerate.


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A real dialog: smaller than the viewport in both dimensions.
        ({"x": 8, "y": 12, "width": 300, "height": 200}, (300, 200, 8, 12)),
        # Fractional CSS pixels round outwards so no painted pixel is lost.
        ({"x": 8.6, "y": 12.4, "width": 300.2, "height": 200.9}, (301, 202, 8, 12)),
        # Full-bleed content: >= the degenerate ratio in BOTH dimensions.
        ({"x": 0, "y": 0, "width": 640, "height": 480}, None),
        ({"x": 0, "y": 0, "width": 632, "height": 474}, None),
        # Full-width but short (a banner) is real geometry, not degenerate.
        ({"x": 0, "y": 0, "width": 640, "height": 120}, (640, 120, 0, 0)),
        # Nothing usable.
        (None, None),
        ({"x": 0, "y": 0, "width": 0, "height": 200}, None),
        ({"x": 0, "y": 0, "width": -300, "height": 200}, None),
        ({"x": 0, "y": 0, "width": float("inf"), "height": 200}, None),
        ({"x": 0, "y": 0, "width": "300", "height": 200}, None),
        ({"x": 0, "y": 0, "width": 300}, None),
    ],
)
def test_parse_content_box(raw, expected):
    if isinstance(raw, dict):
        raw = {**raw, "viewportWidth": 640, "viewportHeight": 480}
    assert _parse_content_box(raw) == expected


def test_popup_bbox_degenerate_ratio_is_a_named_threshold():
    assert 0.9 < POPUP_BBOX_DEGENERATE_RATIO < 1.0


async def _content_box_of(html: str) -> tuple[int, int, int, int] | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        page = await context.new_page()
        await page.set_content(html)
        try:
            return await _popup_content_box(page)
        finally:
            await context.close()
            await browser.close()


async def test_popup_content_box_measures_a_small_dialog():
    # ``body`` is a block: it is 640 wide whatever its children do. Measuring the
    # children (and descending through transparent wrappers) is the whole point.
    box = await _content_box_of(
        "<body style='margin:0'>"
        "<div style='width:100%'>"
        "<div style='width:320px;height:240px;background:#fff'>dialog</div>"
        "</div></body>"
    )
    assert box is not None
    width, height, x, y = box
    assert (x, y) == (0, 0)
    assert width == pytest.approx(320, abs=4)
    assert height == pytest.approx(240, abs=4)


async def test_popup_content_box_rejects_full_bleed_content():
    # A 100vw/100vh wrapper genuinely fills the frame: no honest crop exists, so
    # level 2 must decline instead of returning the viewport back.
    box = await _content_box_of(
        "<body style='margin:0'><div style='width:100vw;height:100vh;background:#123'>x</div></body>"
    )
    assert box is None


async def test_popup_content_box_rejects_a_page_that_paints_its_own_background():
    """A popup styling ``body`` is full-bleed: its background IS the window.

    Found on a real recording — a featureless ``window.open`` of a page with
    ``body { background: yellow }`` filled the whole canvas, and measuring only
    body's children cropped the frame down to the black text on top of it.
    """
    box = await _content_box_of(
        "<body style='margin:0;background:rgb(230,210,20)'>"
        "<h1>Logowanie</h1><label>E-mail</label><input>"
        "</body>"
    )
    assert box is None


async def test_popup_content_box_still_measures_an_unstyled_page():
    """The white of a plain document is the canvas, not a painted background."""
    box = await _content_box_of(
        "<body style='margin:0'><div style='width:240px;height:180px;background:#eee'>d</div></body>"
    )
    assert box is not None
    assert box[0] == pytest.approx(240, abs=4)


async def test_popup_content_box_ignores_guidebot_overlay_elements():
    # The cursor overlay is injected into the popup too; it must not inflate the
    # measured window.
    box = await _content_box_of(
        "<body style='margin:0'>"
        "<div style='width:200px;height:150px;background:#fff'>dialog</div>"
        "<div data-guidebot-cursor style='position:fixed;left:600px;top:460px;"
        "width:20px;height:20px;background:red'></div>"
        "</body>"
    )
    assert box is not None
    width, height, _, _ = box
    assert width < 400 and height < 300, box


async def test_popup_content_box_is_bounded_on_a_huge_dom():
    """The walk is capped in the page, so a monstrous DOM cannot stall the shot."""
    started = time.monotonic()
    box = await _content_box_of(
        "<body style='margin:0'><div id='root'></div><script>"
        "const root = document.getElementById('root');"
        "for (let i = 0; i < 20000; i++) {"
        "  const d = document.createElement('div');"
        "  d.textContent = 'x' + i;"
        "  root.appendChild(d);"
        "}</script></body>"
    )
    elapsed = time.monotonic() - started

    # Whatever it answers (a box, or None because it ran out of budget), it must
    # answer fast: this cost would otherwise land in the recorded video.
    assert elapsed < 2.0, f"content-box walk was not bounded: {elapsed:.2f}s"
    assert box is None or box[0] > 0


# --- popup crop level 2: the measurement must never hang the render ----------
# Same failure mode the window.open lookup had: ``Page.evaluate`` has no timeout,
# so a document that never commits an execution context never answers.


class _HangingPage:
    """A page whose evaluate never resolves."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def evaluate(self, expression, *args):
        self.entered.set()
        await asyncio.Event().wait()


class _SlowPage:
    """A page that answers, but only long after the caller stopped waiting."""

    async def evaluate(self, expression, *args):
        await asyncio.sleep(30)
        return {"x": 0, "y": 0, "width": 10, "height": 10}


def _popup_with_probe(page):
    """A ``_PopupSession`` carrying only what the measurement path touches."""
    session = object.__new__(render_module._PopupSession)
    session.content_box = None
    session.content_box_probe = render_module._start_popup_content_box(page)
    return session


async def test_settle_popup_content_box_gives_up_on_a_page_that_never_answers(monkeypatch, capsys):
    monkeypatch.setattr(render_module.popup_crop, "_POPUP_CONTENT_BOX_TIMEOUT", 0.3)
    page = _HangingPage()
    popup = _popup_with_probe(page)

    started = time.monotonic()
    await render_module._settle_popup_content_box(popup)
    elapsed = time.monotonic() - started

    assert page.entered.is_set(), "test did not exercise the hanging page"
    assert popup.content_box is None
    assert elapsed < 5.0, f"measurement was not bounded: took {elapsed:.1f}s"
    assert "OSTRZEŻENIE" in capsys.readouterr().err


async def test_settle_popup_content_box_abandons_no_orphan_task(monkeypatch):
    monkeypatch.setattr(render_module.popup_crop, "_POPUP_CONTENT_BOX_TIMEOUT", 0.3)
    popup = _popup_with_probe(_SlowPage())

    await render_module._settle_popup_content_box(popup)

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert all(task.done() for task in pending)


async def test_settle_popup_content_box_keeps_a_timely_answer():
    class _PromptPage:
        async def evaluate(self, expression, *args):
            return {
                "x": 4,
                "y": 6,
                "width": 320,
                "height": 240,
                "viewportWidth": 640,
                "viewportHeight": 480,
            }

    popup = _popup_with_probe(_PromptPage())
    await render_module._settle_popup_content_box(popup)

    assert popup.content_box == (320, 240, 4, 6)
    assert popup.content_box_probe is None


async def test_starting_the_content_box_probe_does_not_block():
    """Its cost must be off camera: starting it returns at once."""

    class _SlowishPage:
        async def evaluate(self, expression, *args):
            await asyncio.sleep(0.5)
            return None

    started = time.monotonic()
    probe = render_module._start_popup_content_box(_SlowishPage())
    elapsed = time.monotonic() - started

    assert elapsed < 0.05, f"starting the probe blocked for {elapsed:.3f}s"
    assert not probe.done()
    await asyncio.gather(probe, return_exceptions=True)


async def test_popup_content_box_survives_a_closed_page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        page = await context.new_page()
        await page.close()
        assert await _popup_content_box(page) is None
        await context.close()
        await browser.close()


# --- popup crop: the three-level fallback chain ------------------------------


def test_resolve_popup_crop_prefers_the_window_open_features(tmp_path, capsys):
    crop, level = _resolve_popup_crop(
        window_size=(420, 300),
        content_box=(320, 240, 4, 6),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (420, 300, 0, 0)
    assert level == "window.open"
    assert "window.open" in capsys.readouterr().out


def test_resolve_popup_crop_falls_back_to_the_content_box(tmp_path, capsys):
    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=(320, 240, 4, 6),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (320, 240, 4, 6)
    assert level == "bbox"
    assert "bbox" in capsys.readouterr().out


def test_resolve_popup_crop_falls_back_to_cropdetect(tmp_path, monkeypatch, capsys):
    seen: list[Path] = []

    def fake_detect(path):
        seen.append(Path(path))
        return (300, 220, 2, 2)

    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", fake_detect)

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=None,
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (300, 220, 2, 2)
    assert level == "cropdetect"
    assert seen == [tmp_path / "popup.webm"]
    assert "cropdetect" in capsys.readouterr().out


def test_resolve_popup_crop_without_any_geometry_emits_no_crop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", lambda path: None)

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=None,
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    # Back-compat: today's full-canvas filtergraph, and the log says so.
    assert crop is None
    assert level == "none"
    assert "cropdetect" in capsys.readouterr().out


def test_resolve_popup_crop_does_not_run_cropdetect_when_a_level_answered(tmp_path, monkeypatch):
    def forbidden(path):  # pragma: no cover - failure path
        raise AssertionError("cropdetect must stay the last resort")

    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", forbidden)

    assert (
        _resolve_popup_crop(
            window_size=None,
            content_box=(320, 240, 0, 0),
            popup_video=tmp_path / "popup.webm",
            verbose=False,
        )[1]
        == "bbox"
    )


def test_resolve_popup_crop_scales_the_window_open_rect_into_recording_pixels(
    tmp_path, monkeypatch, capsys
):
    # A headed browser on a HiDPI screen composites the popup at the screen's
    # backing scale, so Playwright fits a 500x670 window into the 1376x800 canvas
    # at 1.19x instead of 1:1 — the CSS rect would cut 96px off the right and
    # 130px off the bottom.
    monkeypatch.setattr(
        render_module.popup_crop, "detect_content_crop", lambda path: (596, 800, 0, 0)
    )

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=True,
    )

    assert crop == (596, 800, 0, 0)
    assert level == "window.open"
    assert "1.19" in capsys.readouterr().out


def test_resolve_popup_crop_scales_the_content_box_into_recording_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(
        render_module.popup_crop, "detect_content_crop", lambda path: (1000, 800, 0, 0)
    )

    crop, level = _resolve_popup_crop(
        window_size=None,
        content_box=(320, 240, 4, 6),
        viewport=(500, 400),
        canvas=(1000, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    # Scale 2x, origin floored and far edges ceiled so rounding never shaves ink.
    assert crop == (640, 480, 8, 12)
    assert level == "bbox"


def test_resolve_popup_crop_keeps_css_pixels_when_the_recording_is_unscaled(tmp_path, monkeypatch):
    # Headless: the popup records 1:1, so ``detect_content_crop`` finds the whole
    # window already trimmed and declines. The CSS rect is then already correct.
    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", lambda path: None)

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    assert crop == (500, 670, 0, 0)
    assert level == "window.open"


@pytest.mark.parametrize(
    "measured",
    [
        (900, 300, 0, 0),  # aspect contradicts the 500x670 window
        (596, 800, 10, 4),  # Playwright anchors the window at the origin
        (120, 160, 0, 0),  # 0.24x — no backing scale produces it
    ],
)
def test_resolve_popup_crop_refuses_a_measurement_that_contradicts_the_viewport(
    tmp_path, monkeypatch, measured
):
    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", lambda path: measured)

    crop, level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=(500, 670),
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    # A measurement that cannot be a scaled popup window is not evidence about
    # the scale; fall back to 1:1 rather than crop to a guess.
    assert crop == (500, 670, 0, 0)
    assert level == "window.open"


def test_resolve_popup_crop_skips_the_scale_probe_without_a_viewport(tmp_path, monkeypatch):
    def forbidden(path):  # pragma: no cover - failure path
        raise AssertionError("nothing to correct against, so nothing to measure")

    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", forbidden)

    crop, _level = _resolve_popup_crop(
        window_size=(500, 670),
        content_box=None,
        viewport=None,
        canvas=(1376, 800),
        popup_video=tmp_path / "popup.webm",
        verbose=False,
    )

    assert crop == (500, 670, 0, 0)


# --- _popup_fills_canvas --------------------------------------------------------


def test_popup_fills_canvas_for_a_declined_crop():
    # Every level declining is the `_blank` tab case: no witness could name a
    # smaller window, so the recording *is* the window.
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas(None, Viewport(width=1376, height=800)) is True


def test_popup_fills_canvas_for_a_full_cover_rect():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 0, 0), Viewport(width=1376, height=800)) is True


def test_popup_does_not_fill_canvas_for_a_real_window():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((520, 640, 0, 0), Viewport(width=1376, height=800)) is False


def test_popup_does_not_fill_canvas_when_offset():
    from guidebot_recorder.models.config import Viewport

    assert _popup_fills_canvas((1376, 800, 12, 12), Viewport(width=1376, height=800)) is False
