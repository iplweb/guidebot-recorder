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

    For a `select` this is the *control* — the field the reader is in — which is
    not where its cursor ends up; see `cursor_shape`.
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


def cursor_shape(
    action: str,
    *,
    box: dict | None,
    row_box: dict | None = None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> Shape | None:
    """The shape the cursor is left inside once this step is done.

    Usually the target's own shape, so the two coincide. A `select:` step
    photographed with its list open is the exception: the cursor ends on the
    **option row** it is about to click, not on the control, so that is where
    this step's arrow has to stop and where the next step's arrow has to start.

    One function decides that, and both users take it from here: `annotations_for`
    clips the incoming arrow against it, and `capture.py` remembers it as
    ``prev_shape``. Pure and cheap on purpose — recomputing beats threading a
    tuple back out of `annotations_for`.
    """

    if row_box is not None:
        return rect_from_box(row_box)
    return target_shape(action, box=box, mark=mark, bounds=bounds)


def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    prev_shape: Shape | None = None,
    center: _Point | None,
    box: dict | None,
    row_box: dict | None = None,
    row_center: _Point | None = None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> list[Annotation]:
    """Build the marks for one target action, omitting any mark that lacks geometry.

    ``row_box``/``row_center`` describe the option row of a ``select:`` step whose
    list was photographed **open**, and they are what splits that one action's
    marks across two boxes: the **frame** stays on the control, so the reader sees
    which field they are in, while the **star** and the arrow's tip go to the row,
    because clicking that row is literally what happens next. Every other action
    puts all three on the same box.

    With no row geometry — ``mode: native``, where the option list is an OS popup
    no screenshot can hold — a ``select`` is marked like any other framed action:
    an arrow to the control's frame and the frame itself, with no star, because
    nothing visible is being clicked.
    """

    anns: list[Annotation] = []
    shape = target_shape(action, box=box, mark=mark, bounds=bounds)
    # Where the reader's eye is being sent, which is the option row when a list
    # was unfurled and the target itself otherwise.
    tip = row_center if row_center is not None else center
    tip_shape = cursor_shape(action, box=box, row_box=row_box, mark=mark, bounds=bounds)

    if prev_cursor is not None and tip is not None:
        # Clipped against both targets, so the arrow runs between them instead of
        # striking each one through. Degenerate results are dropped entirely.
        segment = clipped_arrow(prev_cursor, tip, start_shape=prev_shape, end_shape=tip_shape)
        if segment is not None:
            (x1, y1), (x2, y2) = segment
            anns.append(Annotation(kind="arrow", x1=x1, y1=y1, x2=x2, y2=y2))

    if action in _FRAMED and isinstance(shape, Rect):
        anns.append(Annotation(kind="frame", x=shape.x, y=shape.y, w=shape.w, h=shape.h))

    if action == "click" and center is not None:
        anns.append(_star(center))
    elif action == "select" and row_center is not None:
        # The same star, at the same size, as a `click:` step — because the reader
        # is being told to do the same thing. A star locates its target by its
        # centre rather than by outlining it, so it stays unambiguous on an option
        # row a couple of dozen pixels tall, where an outline would have enclosed
        # its neighbours too.
        anns.append(_star(row_center))
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


def _star(center: _Point) -> Annotation:
    """The click star, at the one size the whole document uses."""

    return Annotation(
        kind="click",
        cx=center[0],
        cy=center[1],
        r_inner=CLICK_INNER,
        r_outer=CLICK_OUTER,
    )
