"""Compose GuidePage list into one landscape HTML document for Chromium page.pdf().

Each :class:`GuidePage` becomes one ``.scene`` — a margin number, the still, and
the narration beside it. Scenes flow rather than each claiming a sheet: two fit
on an A4-landscape page, and a scene whose text runs long pushes its neighbour to
the next sheet instead of being cut.
"""

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
/* Two scenes to a sheet, stacked, rather than one scene per sheet.
   Columns: the margin number, the still, then the narration.

   The last track is `minmax(0, 1fr)` and NOT a percentage on purpose. The
   previous `62% 38%` summed to 100% and then `gap` was added on top, so the
   right-hand column ran off the paper by exactly the gap and Chromium clipped
   whatever text crossed the edge — two or three characters off every line, and
   silently ("Ze względów bezpieczeństw" for "…bezpieczeństwa"). A `fr` track
   absorbs what the fixed tracks and the gaps leave, so the row cannot overflow.
   The `0` floor matters too: plain `1fr` means `minmax(auto, 1fr)`, whose
   min-content floor a long unbreakable word would push past the same edge.

   The still takes half the row's width. A 16:9 still then prints about 36% of
   the printable height — not the 40-50% the width sits at, because on a
   landscape sheet those two bounds cannot both hold for 16:9: a still 45% of
   the height would have to be ~60% of the width. Width is the one that decides
   whether a reader can read the screenshot, so width is the one pinned.

   `min-height` exists only to cap the sheet at two scenes; it is deliberately
   just above a third of the printable height, not half of it. Half would pad
   every short scene with a band of white as tall as a quarter of the picture. */
.scene { display: grid; grid-template-columns: 9mm 50% minmax(0, 1fr); gap: 0 4mm;
         align-items: start; min-height: 78mm; padding-bottom: 4mm;
         break-inside: avoid; page-break-inside: avoid; }
/* Separator between two scenes sharing a sheet. It rides on the *second* scene,
   so a scene that got pushed to a fresh sheet carries a rule along its top —
   cosmetic, and the alternative (a rule under the first) would instead strand
   one at the foot of a sheet whose partner moved on. */
.scene + .scene { border-top: 1px solid #d0d0d0; padding-top: 4mm; }
/* Sequential position of this scene in the finished PDF — deliberately not the
   scenario's step index, which counts `say:`-only steps and other things that
   never became a picture. Set by `render_html`, which is the only place that
   knows what actually got rendered. */
.num { font-size: 15px; font-weight: 700; color: #8a8a8a; padding-top: 1mm;
       text-align: right; }
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
.side { padding-top: 1mm; }
/* A hair smaller than the one-scene-per-sheet layout these replaced: two scenes
   share the height that one used to have, and the narration column is now the
   wider of the two (~128mm against the old ~104mm), so the lines still break in
   roughly the same places. */
.side .heading { font-size: 18px; font-weight: 700; margin: 0 0 3mm; }
.side .body { font-size: 15px; line-height: 1.45; white-space: pre-wrap; }
/* Separates this step's own description from narration folded in from steps
   that had nothing to photograph — see `_fold_narration`. */
.side hr.fold { border: 0; border-top: 1px solid #d6d6d6; margin: 3mm 0; }
/* A slide is a full-sheet interstitial: it keeps a page to itself and pushes
   whatever follows onto the next one. */
.slide { display: flex; flex-direction: column; justify-content: center;
         height: 100vh; text-align: center; break-after: page; page-break-after: always; }
.slide .title { font-size: 40px; font-weight: 800; }
.slide .subtitle { font-size: 24px; color: #555; margin-top: 4mm; }
.arrow { stroke: #e11; stroke-width: 4; fill: none; marker-end: url(#ah); }
.star { stroke: #e11; stroke-width: 4; fill: none; stroke-linecap: round; }
/* No `.frame` rule: the target outline is painted into the page before the
   screenshot (see `stills._framed_screenshot`), because an SVG rectangle over
   the finished PNG would cover the cursor. */
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


def _shot_scene(page: GuidePage, number: int) -> str:
    uri = Path(page.screenshot).absolute().as_uri()
    svg = _svg(page.annotations, page.screenshot_size or (1, 1))
    shot_style, plate_style = _letterbox(page)
    return (
        '<section class="scene">'
        f'<div class="num">{number}</div>'
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
    return f'<section class="slide">{title}{sub}</section>'


def _text_scene(page: GuidePage, number: int) -> str:
    """Narration that found no picture to join — spans the still and text columns.

    Reached only when :func:`fold_narration` declined to fold it: nothing came
    before it, or what came before was a slide. It keeps its margin number, so the
    numbering stays the count of scenes a reader can actually see.
    """

    return (
        '<section class="scene">'
        f'<div class="num">{number}</div>'
        f'<div class="side" style="grid-column:2/-1">{_side(page)}</div>'
        "</section>"
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
    """Render exactly the pages given — one ``<section class="scene">`` each.

    Scenes *flow*: two fit on an A4-landscape sheet, and one whose narration runs
    long simply takes the room it needs and pushes its neighbour onto the next
    sheet (``break-inside: avoid`` keeps a scene whole). Sheet count is therefore
    the printer's answer, not this function's — which is why the caller counts
    scenes and says so.

    Numbering runs over the scenes this renderer actually emits, skipping slides:
    a reader counting pictures on paper has no way to see a `say:`-only step, so
    numbering by scenario step index would visibly skip.

    Does **not** fold; run :func:`fold_narration` first if you want that.
    """

    body_parts: list[str] = []
    number = 0
    for page in pages:
        if page.kind == "slide":
            body_parts.append(_slide_page(page))
            continue
        number += 1
        if page.screenshot is not None:
            body_parts.append(_shot_scene(page, number))
        else:
            body_parts.append(_text_scene(page, number))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )
