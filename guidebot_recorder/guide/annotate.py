"""Pure annotation geometry — no I/O, no browser. Coordinates are screenshot pixels."""

from __future__ import annotations

from guidebot_recorder.guide.model import Annotation
from guidebot_recorder.models.scenario import ResolvedHighlight
from guidebot_recorder.overlay.geometry import ellipse_around, fit_to_bounds

#: fixed click-circle radius (screenshot px, deviceScaleFactor already applied upstream)
CLICK_RADIUS = 22.0

#: Floor under the click circle when it is clamped to a short target (see
#: :func:`annotations_for`). Below this a printed circle stops reading as a mark
#: and starts reading as a smudge.
MIN_CLICK_RADIUS = 8.0

_Point = tuple[float, float]


def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    center: _Point | None,
    box: dict | None,
    row_box: dict | None = None,
    row_center: _Point | None = None,
    mark: ResolvedHighlight | None = None,
    bounds: _Point | None = None,
) -> list[Annotation]:
    """Build the marks for one target action, omitting any mark that lacks geometry.

    ``row_box``/``row_center`` describe the option row of a ``select:`` step whose
    list was photographed **open**. They move where the reader is sent: the arrow
    ends on the row rather than on the control, and the row gets the same click
    circle a ``click:`` step draws — because clicking that row is literally what
    happens next. The ``selected`` rectangle stays on the *control*, so the reader
    can still see which field they are in.

    The circle is clamped to half the row's height so it can never spill onto the
    neighbouring options: option rows are only a couple of dozen pixels apart, and
    a fixed radius would draw one mark across three of them, which reads as
    pointing at all three.

    With no row geometry — ``mode: native``, where the option list is an OS popup
    no screenshot can hold — a ``select`` produces exactly the marks it always
    did: an arrow to the control and a rectangle around it.
    """

    anns: list[Annotation] = []
    # The arrow ends wherever the reader's eye is being sent, which is the option
    # row when there is one and the control otherwise.
    tip = row_center if row_center is not None else center
    if prev_cursor is not None and tip is not None:
        anns.append(
            Annotation(kind="arrow", x1=prev_cursor[0], y1=prev_cursor[1], x2=tip[0], y2=tip[1])
        )
    if action == "click" and center is not None:
        anns.append(Annotation(kind="click", cx=center[0], cy=center[1], r=CLICK_RADIUS))
    elif action == "hover" and box is not None:
        anns.append(
            Annotation(kind="hover", x=box["x"], y=box["y"], w=box["width"], h=box["height"])
        )
    elif action == "type" and box is not None:
        anns.append(
            Annotation(kind="typed", x=box["x"], y=box["y"], w=box["width"], h=box["height"])
        )
    elif action == "select":
        if box is not None:
            anns.append(
                Annotation(kind="selected", x=box["x"], y=box["y"], w=box["width"], h=box["height"])
            )
        if row_center is not None:
            anns.append(
                Annotation(
                    kind="click",
                    cx=row_center[0],
                    cy=row_center[1],
                    r=_row_click_radius(row_box),
                )
            )
    elif action == "highlight" and box is not None and mark is not None:
        # The same ellipse the film laps the cursor around — shared geometry, so
        # the page and the recording mark the target identically. `bounds` is the
        # screenshot: without the fit the ellipse would be clipped by `.shot`.
        ellipse = ellipse_around(box, mark.padding)
        if bounds is not None:
            ellipse = fit_to_bounds(ellipse, width=bounds[0], height=bounds[1])
        anns.append(
            Annotation(
                kind="highlight",
                cx=ellipse.cx,
                cy=ellipse.cy,
                rx=ellipse.rx,
                ry=ellipse.ry,
                color=mark.color,
            )
        )
    return anns


def _row_click_radius(row_box: dict | None) -> float:
    """Click-circle radius that stays inside one option row."""

    if row_box is None:
        return CLICK_RADIUS
    return max(MIN_CLICK_RADIUS, min(CLICK_RADIUS, row_box["height"] / 2))
