"""E2E: the PDF guide photographs a `select:` step with its option list open.

The central claim of the branch is a claim about a *picture*, so the central test
is about pixels: the frame the guide kept for a `select:` step must contain the
unfurled list, and the click mark the PDF draws on that frame must land on the
option row. Asserting that a code path ran, or that the final value is right,
would leave both of those free to be wrong.

The fixture puts a large block of one saturated colour directly under the raw
``<select>`` (see ``guide-select.html``). The shim's list opens downward over it,
so "was the list in frame?" becomes "is that colour still there?" — a question
that needs no knowledge of how the list is styled and no image dependency.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from playwright.async_api import Browser, async_playwright

import guidebot_recorder.guide.guide as guide_module
from guidebot_recorder.guide.guide import run_guide
from guidebot_recorder.guide.model import Annotation, GuidePage
from guidebot_recorder.models.target import TestidTarget
from guidebot_recorder.recorder.compile import run_compile_in_browser
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import install_selects

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "guide-select.html"

#: The flat colour ``guide-select.html`` paints under the raw ``<select>``.
BACKDROP = (0, 153, 102)

SHIMMED_SCENARIO = """\
config:
  title: Przewodnik z listą
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX"}}
    say: "Wybieram format BibTeX."
"""

#: The Tom Select pattern — a ``display: none`` original with a visible widget.
#: Before this branch the guide drove it with ``select_option`` against the
#: hidden original and timed out on Playwright's actionability wait.
ENHANCED_SCENARIO = """\
config:
  title: Przewodnik z widżetem
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - navigate: "{url}"
  - select: {{from: "lista miast", option: "Lublin"}}
    say: "Wybieram Lublin."
"""

NATIVE_SCENARIO = """\
config:
  title: Przewodnik bez nakładki
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX", mode: native}}
    say: "Wybieram format BibTeX."
"""

_SELECT_TARGETS = {"formatów": "format", "miast": "miasto"}


class SelectReasoner:
    """Resolves every instruction in this suite to a fixed, frozen target."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        for fragment, testid in _SELECT_TARGETS.items():
            if fragment in instruction:
                return ReasonerResult("select", TestidTarget(testid=testid))
        raise AssertionError(f"nieoczekiwana instrukcja: {instruction!r}")


# --- a minimal PNG reader ---------------------------------------------------
# The project has no image dependency and does not gain one for a test. Only
# what Playwright actually writes is supported: 8-bit, non-interlaced, RGB or
# RGBA.


def _read_png(path: Path) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Decode a PNG into row-major ``(r, g, b)`` tuples."""

    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat = 8, bytearray()
    width = height = channels = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
            assert depth == 8 and interlace == 0 and color_type in (2, 6), "unsupported PNG"
            channels = 3 if color_type == 2 else 4
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[list[tuple[int, int, int]]] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif filter_type == 2:
            for x in range(stride):
                line[x] = (line[x] + previous[x]) & 0xFF
        elif filter_type == 3:
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + previous[x]) >> 1)) & 0xFF
        elif filter_type == 4:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = previous[x]
                c = previous[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + predictor) & 0xFF
        rows.append(
            [
                (line[x * channels], line[x * channels + 1], line[x * channels + 2])
                for x in range(width)
            ]
        )
        previous = line
    return width, height, rows


def _backdrop_share(path: Path, rect: dict, *, inset: int = 3) -> float:
    """Fraction of ``rect`` in this screenshot still painted the backdrop colour."""

    _width, _height, rows = _read_png(path)
    x0 = int(rect["x"]) + inset
    y0 = int(rect["y"]) + inset
    x1 = int(rect["x"] + rect["width"]) - inset
    y1 = int(rect["y"] + rect["height"]) - inset
    assert x1 > x0 and y1 > y0, f"rect too small to sample: {rect}"
    total = hits = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            total += 1
            if rows[y][x] == BACKDROP:
                hits += 1
    return hits / total


# --- helpers ----------------------------------------------------------------


def _write(tmp_path: Path, name: str, template: str) -> Path:
    path = tmp_path / name
    path.write_text(template.format(url=FIXTURE.resolve().as_uri()), encoding="utf-8")
    return path


async def _guide_with_pages(
    path: Path, out: Path, browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> list[GuidePage]:
    """Run the real ``run_guide`` and keep the ``GuidePage`` list it builds.

    ``run_guide`` returns only a page count and closes its context, so the marks
    it computed would otherwise be observable only through the finished PDF.
    """

    kept: list[list[GuidePage]] = []
    original = guide_module.capture_pages

    async def spy(*args, **kwargs):
        pages = await original(*args, **kwargs)
        kept.append(pages)
        return pages

    monkeypatch.setattr(guide_module, "capture_pages", spy)
    await run_guide(path, out, browser, timeout=10.0)
    assert kept, "capture_pages was never called"
    return kept[0]


def _only(annotations: list[Annotation], kind: str) -> Annotation:
    matching = [a for a in annotations if a.kind == kind]
    assert len(matching) == 1, f"expected exactly one {kind!r}, got {annotations}"
    return matching[0]


async def _measure_open_list(browser: Browser, path: Path) -> tuple[dict, dict, Path]:
    """Measure the fixture independently of the run under test.

    Returns the ``BibTeX`` option row's rect, the backdrop's rect, and a
    reference screenshot taken with the list still **closed** — the frame the
    guide would have kept if it had not opened anything.
    """

    scenario = load_scenario(path, None)
    context = await browser.new_context(viewport={"width": 800, "height": 600})
    try:
        selects = await install_selects(context, scenario.config)
        assert selects is not None
        page = await context.new_page()
        await page.goto(FIXTURE.resolve().as_uri())
        await selects.wait_ready(page)

        backdrop = await page.locator("#backdrop").bounding_box()
        closed = path.parent / "closed.png"
        await page.screenshot(path=str(closed))

        select = page.locator("#format")
        index = await select.evaluate(
            "(el, label) => window.__guidebot_selects.optionIndexFor(el, label)", "BibTeX"
        )
        assert index == 2
        await select.evaluate("(el) => window.__guidebot_selects.open(el)")
        uid = await select.get_attribute("data-guidebot-shimmed")
        row = page.locator(
            f'[data-guidebot-select-list][data-guidebot-for="{uid}"]'
            f' [data-guidebot-option-index="{index}"]'
        )
        await row.wait_for(state="visible", timeout=5000)
        row_box = await row.bounding_box()
    finally:
        await context.close()
    assert row_box is not None and backdrop is not None
    return row_box, backdrop, closed


# --- the central test -------------------------------------------------------


async def test_the_frame_shows_the_open_list_and_the_click_mark_sits_on_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `select:` page's screenshot contains the unfurled list, and the click
    circle the PDF draws on it falls inside the option row.

    Both halves matter and neither implies the other. A frame taken with the
    list open but annotated from the collapsed control's stale box would mark
    the wrong thing; a mark computed from a row that was never photographed
    would point at nothing.
    """

    path = _write(tmp_path, "shimmed.scenario.yaml", SHIMMED_SCENARIO)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await run_compile_in_browser(path, browser, SelectReasoner())
            pages = await _guide_with_pages(path, tmp_path / "guide.pdf", browser, monkeypatch)
            row, backdrop, closed = await _measure_open_list(browser, path)
        finally:
            await browser.close()

    # The fixture's premise: the row the list draws sits over the flat colour
    # block, so "is that colour still there?" answers "was the list in frame?".
    assert (
        backdrop["y"] <= row["y"] and row["y"] + row["height"] <= backdrop["y"] + backdrop["height"]
    )
    assert _backdrop_share(closed, row) > 0.98, "the reference frame should be all backdrop"

    # navigate + select
    assert [p.kind for p in pages] == ["navigate", "step"]
    select_page = pages[1]
    assert select_page.screenshot is not None

    # --- the frame really shows the list ---
    assert _backdrop_share(select_page.screenshot, row) == 0.0, (
        "the backdrop is still visible where the option row should be — "
        "the frame was taken with the list closed"
    )

    # --- the click mark really sits on the option row ---
    click = _only(select_page.annotations, "click")
    assert row["x"] <= click.cx <= row["x"] + row["width"]
    assert row["y"] <= click.cy <= row["y"] + row["height"]


async def test_the_select_page_frames_the_control_and_points_the_arrow_at_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other two marks: a rectangle around the control the reader is in, and
    an arrow that ends on the option row rather than on the control."""

    path = _write(tmp_path, "shimmed-marks.scenario.yaml", SHIMMED_SCENARIO)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await run_compile_in_browser(path, browser, SelectReasoner())
            pages = await _guide_with_pages(path, tmp_path / "marks.pdf", browser, monkeypatch)
            row, _backdrop, _closed = await _measure_open_list(browser, path)
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            await page.goto(FIXTURE.resolve().as_uri())
            control = await page.locator("#format").bounding_box()
            await context.close()
        finally:
            await browser.close()

    assert control is not None
    selected = _only(pages[1].annotations, "selected")
    assert (selected.x, selected.y) == pytest.approx((control["x"], control["y"]), abs=1.0)
    assert (selected.w, selected.h) == pytest.approx((control["width"], control["height"]), abs=1.0)
    # The arrow starts at the previous cursor position, which the navigate step
    # cleared, so this page has none — the first `select:` after a navigate is
    # exactly the case where an arrow must not be invented.
    assert not [a for a in pages[1].annotations if a.kind == "arrow"]
    # ...but the row is what the *next* step's arrow will start from.
    assert row["x"] <= _only(pages[1].annotations, "click").cx <= row["x"] + row["width"]


async def test_a_display_none_widget_is_driven_instead_of_timing_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight gap of the design's §6, closed.

    ``validate_compile_time``'s ``select`` arm accepts a page-enhanced select
    because render drives the page's own widget. The guide inherited that
    loosening through ``reuse_failure`` and then ran ``select_option`` against
    the hidden original: measured, ``reuse_failure`` returned ``None``,
    ``recorder.point`` returned a box-less result without raising, and
    ``Locator.select_option`` was what raised first — an English Playwright
    timeout after the full step timeout.

    Driving the widget the way render does removes it: the hidden original is
    never the click target. ``Recorder`` reads the select back after the click,
    so reaching the end of this run is itself the assertion that the choice took.
    """

    path = _write(tmp_path, "enhanced.scenario.yaml", ENHANCED_SCENARIO)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await run_compile_in_browser(path, browser, SelectReasoner())
            pages = await _guide_with_pages(path, tmp_path / "enhanced.pdf", browser, monkeypatch)
        finally:
            await browser.close()

    assert [p.kind for p in pages] == ["navigate", "step"]
    # A click mark at all means a row of the widget's own list was the target;
    # the hidden `<select>` has no row and would have produced none.
    click = _only(pages[1].annotations, "click")
    assert click.cx is not None and click.cy is not None
    # The widget stands where the hidden original is, so the framed control must
    # be the widget's box — a `display: none` element has no box to frame.
    selected = _only(pages[1].annotations, "selected")
    assert selected.w is not None and selected.w > 8


async def test_native_mode_keeps_todays_collapsed_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mode: native` has no list to reveal, and that must not become an error."""

    path = _write(tmp_path, "native.scenario.yaml", NATIVE_SCENARIO)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await run_compile_in_browser(path, browser, SelectReasoner())
            pages = await _guide_with_pages(path, tmp_path / "native.pdf", browser, monkeypatch)
            _row, _backdrop, closed = await _measure_open_list(browser, path)
        finally:
            await browser.close()

    select_page = pages[1]
    assert select_page.screenshot is not None
    # Today's shape exactly: the control is framed and nothing is circled.
    assert {a.kind for a in select_page.annotations} == {"selected"}
    # And nothing was unfurled over the backdrop.
    _width, _height, rows = _read_png(select_page.screenshot)
    reference = _read_png(closed)[2]
    band = range(int(_backdrop["y"]) + 5, int(_backdrop["y"]) + 60)
    assert all(rows[y][400] == reference[y][400] == BACKDROP for y in band)
