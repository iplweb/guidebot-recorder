"""Compose GuidePage list into one landscape HTML document for Chromium page.pdf()."""

from __future__ import annotations

import html
import math
from dataclasses import replace
from pathlib import Path

from guidebot_recorder.guide.model import Annotation, GuidePage

_STYLE = """
@page { size: A4 landscape; margin: 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #1a1a1a; margin: 0; }
.page { display: grid; grid-template-columns: 62% 38%; gap: 6mm; height: 100vh;
        page-break-after: always; align-items: start; }
.page:last-child { page-break-after: auto; }
.shot { position: relative; width: 100%; border: 1px solid #ddd; border-radius: 6px;
        overflow: hidden; display: flex; align-items: center; justify-content: center;
        background: #eceef2; }
/* The still and its annotation layer share one box, so the SVG's viewBox
   coordinates and the image's pixels line up. That box is `.plate`, not `.shot`:
   a popup still does not fill `.shot`, and an overlay pinned to `.shot` would sit
   off the picture by the width of the letterbox margin. At `width: 100%` (every
   main-window page) the two boxes coincide and this renders as it always did. */
.plate { position: relative; width: 100%; }
.plate img { width: 100%; display: block; }
.plate svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.side { padding-top: 2mm; }
.side .heading { font-size: 20px; font-weight: 700; margin: 0 0 4mm; }
.side .body { font-size: 16px; line-height: 1.5; white-space: pre-wrap; }
/* Separates this step's own description from narration folded in from steps
   that had nothing to photograph — see `_fold_narration`. */
.side hr.fold { border: 0; border-top: 1px solid #d6d6d6; margin: 4mm 0; }
.slide { grid-column: 1 / -1; display: flex; flex-direction: column; justify-content: center;
         height: 100vh; text-align: center; }
.slide .title { font-size: 40px; font-weight: 800; }
.slide .subtitle { font-size: 24px; color: #555; margin-top: 4mm; }
.arrow { stroke: #e11; stroke-width: 4; fill: none; marker-end: url(#ah); }
.star { stroke: #e11; stroke-width: 4; fill: none; stroke-linecap: round; }
.frame { stroke: #e11; stroke-width: 4; fill: rgba(238,17,17,0.08); }
/* The marker colour is per step, so only the shape lives here — `stroke` is set
   on the element itself. */
.highlight { stroke-width: 5; fill: none; stroke-linecap: round; }
"""

# `markerUnits` defaults to `strokeWidth`, so the head scales with `.arrow`'s
# `stroke-width: 4`: a 6-wide marker paints ~24 screenshot px. Kept deliberately
# small — a bigger head swallowed short clipped arrows whole (all head, no shaft).
_ARROW_MARKER = (
    '<defs><marker id="ah" markerWidth="6" markerHeight="6" refX="5" refY="3" '
    'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#e11"/></marker></defs>'
)


_STAR_ARMS = 8


def _star(a: Annotation) -> list[str]:
    """`_STAR_ARMS` arms evenly spaced, each spanning `r_inner`..`r_outer` around (`cx`, `cy`).

    Coordinates are rounded to two decimals so the HTML does not swell with
    17-digit floats.
    """

    cx, cy = a.cx or 0.0, a.cy or 0.0
    inner, outer = a.r_inner or 0.0, a.r_outer or 0.0
    lines = []
    for i in range(_STAR_ARMS):
        angle = 2 * math.pi * i / _STAR_ARMS
        dx, dy = math.cos(angle), math.sin(angle)
        lines.append(
            f'<line class="star" x1="{round(cx + dx * inner, 2)}" '
            f'y1="{round(cy + dy * inner, 2)}" x2="{round(cx + dx * outer, 2)}" '
            f'y2="{round(cy + dy * outer, 2)}"/>'
        )
    return lines


def _svg(anns: list[Annotation], size: tuple[int, int]) -> str:
    w, h = size
    parts = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">', _ARROW_MARKER]
    for a in anns:
        if a.kind == "arrow":
            parts.append(f'<line class="arrow" x1="{a.x1}" y1="{a.y1}" x2="{a.x2}" y2="{a.y2}"/>')
        elif a.kind == "click":
            parts.extend(_star(a))
        elif a.kind == "frame":
            parts.append(
                f'<rect class="frame" x="{a.x}" y="{a.y}" width="{a.w}" height="{a.h}" rx="4"/>'
            )
        elif a.kind == "highlight":
            # The colour comes from the scenario, so it is escaped like any other
            # author-supplied text before it lands in an attribute.
            stroke = html.escape(a.color or "#e11", quote=True)
            parts.append(
                f'<ellipse class="highlight" cx="{a.cx}" cy="{a.cy}" '
                f'rx="{a.rx}" ry="{a.ry}" stroke="{stroke}"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _letterbox(page: GuidePage) -> tuple[str, str]:
    """Inline styles that centre a smaller still on the page's canvas.

    Returns ``(shot_style, plate_style)``, both empty when the still already fills
    the canvas — which is every main-window page, so the common case adds no
    markup at all. A popup opens at its own size, and the two sizes differing is
    the only signal that it is one (see :class:`GuidePage`).

    ``.shot`` gets the canvas aspect ratio rather than a pixel height so it keeps
    scaling with the print column, and ``.plate`` is sized as a percentage of it —
    the same ratio the still has to the canvas, so the picture lands unscaled
    relative to its neighbours and a 600px-wide popup reads as narrower than the
    1376px window it came from, which is the honest depiction.
    """

    shot, canvas = page.screenshot_size, page.canvas_size
    if not shot or not canvas or shot == canvas or not all(canvas):
        return "", ""
    width = round(100 * shot[0] / canvas[0], 3)
    height = round(100 * shot[1] / canvas[1], 3)
    return (
        f' style="aspect-ratio:{canvas[0]}/{canvas[1]}"',
        f' style="width:{width}%;height:{height}%"',
    )


def _side(page: GuidePage) -> str:
    """The right-hand column: this step's description, then anything folded in."""

    heading = f'<div class="heading">{html.escape(page.heading)}</div>' if page.heading else ""
    body = f'<div class="body">{html.escape(page.text)}</div>' if page.text else ""
    folded = "".join(
        f'<hr class="fold"/><div class="body">{html.escape(text)}</div>'
        for text in page.folded_text
    )
    return f"{heading}{body}{folded}"


def _shot_page(page: GuidePage) -> str:
    uri = Path(page.screenshot).absolute().as_uri()
    svg = _svg(page.annotations, page.screenshot_size or (1, 1))
    shot_style, plate_style = _letterbox(page)
    return (
        '<section class="page">'
        f'<div class="shot"{shot_style}>'
        f'<div class="plate"{plate_style}><img src="{uri}"/>{svg}</div></div>'
        f'<div class="side">{_side(page)}</div>'
        "</section>"
    )


def _slide_page(page: GuidePage) -> str:
    title = f'<div class="title">{html.escape(page.heading or page.text)}</div>'
    sub = (
        f'<div class="subtitle">{html.escape(page.text)}</div>'
        if page.heading and page.text
        else ""
    )
    return f'<section class="page"><div class="slide">{title}{sub}</div></section>'


def _text_page(page: GuidePage) -> str:
    """Narration that found no picture to join — full width, still its own sheet.

    Reached only when :func:`_fold_narration` declined to fold it: nothing came
    before it, or what came before was a slide.
    """

    return (
        '<section class="page"><div class="side" style="grid-column:1/-1">'
        f"{_side(page)}</div></section>"
    )


def _folds_into_previous(page: GuidePage, previous: GuidePage | None) -> bool:
    """Whether ``page`` is narration that belongs on the picture before it.

    Three conditions, and each excludes a page that must keep its own sheet: a
    narration page with nothing before it has no picture to join; a page with a
    still of its own is the thing being described, not a description; and a slide
    is a deliberate full-page interstitial, so it neither absorbs nor is absorbed.
    """

    return (
        previous is not None
        and previous.screenshot is not None
        and page.screenshot is None
        and page.kind != "slide"
    )


def fold_narration(pages: list[GuidePage]) -> list[GuidePage]:
    """Move picture-less narration onto the page of the still it was spoken over.

    A `say:` between two actions describes what the reader is looking at — it did
    not earn a sheet of paper with a sentence marooned on it. Folding happens here
    and not in the capture pass on purpose: one step still produces one
    :class:`GuidePage`, and pagination is the document's question alone.

    Empty narration folds to nothing rather than to a bare rule, which is also how
    a text page that never had any text stops printing a blank sheet.

    **Public, and called by ``run_guide`` rather than by :func:`render_html`.**
    Folding inside the renderer would leave the caller holding the *unfolded* list
    as its only measure of the document — and `guide` reports that length as the
    page count, so the CLI would cheerfully announce eight pages of a five-page
    PDF. One list, counted and rendered, is the only shape that cannot drift.
    """

    folded: list[GuidePage] = []
    for page in pages:
        previous = folded[-1] if folded else None
        if not _folds_into_previous(page, previous):
            folded.append(page)
            continue
        carried = [*previous.folded_text, page.text] if page.text else previous.folded_text
        folded[-1] = replace(previous, folded_text=carried)
    return folded


def render_html(pages: list[GuidePage], *, title: str) -> str:
    """Render exactly the pages given — one ``<section class="page">`` each.

    Does **not** fold; run :func:`fold_narration` first if you want that. Keeping
    the renderer free of pagination is what lets the caller count the same list it
    prints.
    """

    body_parts: list[str] = []
    for page in pages:
        if page.kind == "slide":
            body_parts.append(_slide_page(page))
        elif page.screenshot is not None:
            body_parts.append(_shot_page(page))
        else:
            body_parts.append(_text_page(page))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )
