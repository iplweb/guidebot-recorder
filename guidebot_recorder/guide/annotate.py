"""Pure annotation geometry — no I/O, no browser. Coordinates are screenshot pixels."""

from __future__ import annotations

from guidebot_recorder.guide.geometry import Rect, Shape, clipped_arrow, rect_from_box
from guidebot_recorder.guide.model import Annotation
from guidebot_recorder.models.scenario import ResolvedHighlight
from guidebot_recorder.overlay.geometry import Ellipse, ellipse_around, fit_to_bounds

#: click star: each of the eight arms spans these radii around the cursor
#: (screenshot px, deviceScaleFactor already applied upstream). The gap left by
#: `CLICK_INNER` clears the 16 px cursor ring drawn by `overlay/cursor.js`, so
#: the cursor itself stays readable inside the star.
CLICK_INNER = 16.0
CLICK_OUTER = 30.0

#: Actions whose target box gets a red frame. `highlight` keeps its own ellipse.
_FRAMED = frozenset({"click", "type", "hover", "select"})

_Point = tuple[float, float]


def target_shape(
    action: str,
    *,
    box: dict | None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> Shape | None:
    """The shape the annotations draw around the target.

    `highlight` -> the ellipse fitted to the screenshot (the very one its
    annotation draws); any other action with a box -> a `Rect` from that box;
    no box -> ``None``. A `highlight` without a `mark` is ``None`` too: the
    padding is unknown, so there is no ellipse to compute — and such a step
    gets no highlight annotation either.

    Pure and cheap on purpose: `capture.py` calls it again to remember the
    previous target, which is clearer than threading a tuple back out of
    `annotations_for`.
    """

    if box is None:
        return None
    if action == "highlight":
        if mark is None:
            return None
        # The same ellipse the film laps the cursor around — shared geometry, so
        # the page and the recording mark the target identically. `bounds` is the
        # screenshot: without the fit the ellipse would be clipped by `.shot`.
        ellipse = ellipse_around(box, mark.padding)
        if bounds is not None:
            ellipse = fit_to_bounds(ellipse, width=bounds[0], height=bounds[1])
        return ellipse
    return rect_from_box(box)


def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    prev_shape: Shape | None = None,
    center: _Point | None,
    box: dict | None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> list[Annotation]:
    """Build the marks for one target action, omitting any mark that lacks geometry."""

    anns: list[Annotation] = []
    shape = target_shape(action, box=box, mark=mark, bounds=bounds)

    if prev_cursor is not None and center is not None:
        # Clipped against both targets, so the arrow runs between them instead of
        # striking each one through. Degenerate results are dropped entirely.
        segment = clipped_arrow(prev_cursor, center, start_shape=prev_shape, end_shape=shape)
        if segment is not None:
            (x1, y1), (x2, y2) = segment
            anns.append(Annotation(kind="arrow", x1=x1, y1=y1, x2=x2, y2=y2))

    if action in _FRAMED and isinstance(shape, Rect):
        anns.append(Annotation(kind="frame", x=shape.x, y=shape.y, w=shape.w, h=shape.h))

    if action == "click" and center is not None:
        anns.append(
            Annotation(
                kind="click",
                cx=center[0],
                cy=center[1],
                r_inner=CLICK_INNER,
                r_outer=CLICK_OUTER,
            )
        )
    elif action == "highlight" and mark is not None and isinstance(shape, Ellipse):
        # `shape` already went through `ellipse_around` / `fit_to_bounds`, so the
        # arrow above and this ellipse cannot drift apart.
        anns.append(
            Annotation(
                kind="highlight",
                cx=shape.cx,
                cy=shape.cy,
                rx=shape.rx,
                ry=shape.ry,
                color=mark.color,
            )
        )
    return anns
