"""Ellipse geometry for the `highlight` command — pure math, no I/O, no browser.

Both consumers share this module on purpose: the film animates the cursor along
the ellipse (`Recorder.highlight`) and the PDF guide draws the same ellipse onto
a screenshot (`guide/annotate.py`). Duplicating the √2 factor in Python and in
`cursor.js` would let the two drift apart at the first tweak.
"""

from __future__ import annotations

import math
from typing import NamedTuple

#: Smallest ellipse *similar to the box* that still contains it: scaling both
#: half-extents by √2 puts the corners exactly on the ellipse.
_CIRCUMSCRIBE = math.sqrt(2.0)


class Ellipse(NamedTuple):
    """Centre and radii, in the pixel space of whatever frame produced the box."""

    cx: float
    cy: float
    rx: float
    ry: float


def ellipse_around(box: dict, padding: float) -> Ellipse:
    """The ellipse that circumscribes ``box`` grown by ``padding`` on every side.

    ``box`` is Playwright's bounding box (``x``/``y``/``width``/``height``).
    """

    half_w = box["width"] / 2 + padding
    half_h = box["height"] / 2 + padding
    return Ellipse(
        cx=box["x"] + box["width"] / 2,
        cy=box["y"] + box["height"] / 2,
        rx=half_w * _CIRCUMSCRIBE,
        ry=half_h * _CIRCUMSCRIBE,
    )


def fit_to_bounds(e: Ellipse, width: float, height: float, margin: float = 4.0) -> Ellipse:
    """Keep the ellipse inside a ``width`` × ``height`` frame.

    Radii are clamped first, then the centre is nudged inwards. Without this the
    cursor would ride off-viewport during the film and the PDF would clip the
    ellipse against `.shot { overflow: hidden }`. A target bigger than the frame
    ends up with an ellipse that crosses it — accepted, there is no better answer.
    """

    rx = min(e.rx, max(0.0, width / 2 - margin))
    ry = min(e.ry, max(0.0, height / 2 - margin))
    return Ellipse(
        cx=min(max(e.cx, margin + rx), width - margin - rx),
        cy=min(max(e.cy, margin + ry), height - margin - ry),
        rx=rx,
        ry=ry,
    )


def ellipse_perimeter(e: Ellipse) -> float:
    """Ramanujan's approximation — accurate well past what cursor pacing needs."""

    a, b = e.rx, e.ry
    h = ((a - b) ** 2) / ((a + b) ** 2) if (a + b) else 0.0
    return math.pi * (a + b) * (1 + (3 * h) / (10 + math.sqrt(4 - 3 * h)))
