"""Arrow-clipping geometry for the PDF guide — pure math, no I/O, no browser.

The guide draws an arrow from the previous target to the current one. Drawn
centre to centre it crosses both targets and reads as a strikethrough rather
than a pointer, so `annotate.py` clips it against the shape each end sits in:
the segment starts where it leaves the previous target and stops where it
enters the current one.

`Ellipse` is imported from `overlay/geometry.py` rather than redefined — a
`highlight` step is marked with exactly the ellipse the film laps the cursor
around, and the arrow has to stop at that same rim.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from guidebot_recorder.overlay.geometry import Ellipse


class Rect(NamedTuple):
    """Top-left corner plus size, in the pixel space of the screenshot."""

    x: float
    y: float
    w: float
    h: float


#: What an annotated target can look like: a frame around a control, or the
#: `highlight` ellipse.
Shape = Rect | Ellipse

#: Shorter than this (screenshot px) an arrow is all head and no shaft, so the
#: guide drops it entirely — see `clipped_arrow`.
MIN_ARROW = 12.0

_Point = tuple[float, float]


def rect_from_box(box: dict) -> Rect:
    """Playwright bounding box (``x``/``y``/``width``/``height``) -> `Rect`."""

    return Rect(x=box["x"], y=box["y"], w=box["width"], h=box["height"])


def ray_exit(origin: _Point, toward: _Point, shape: Shape) -> _Point:
    """The point where the ray ``origin`` -> ``toward`` leaves ``shape``.

    Returns ``origin`` unchanged when ``origin`` is not strictly inside the
    shape, when ``origin == toward``, or when the shape is degenerate (a zero
    extent on either axis). Callers treat that as "nothing to clip here".
    """

    dx = toward[0] - origin[0]
    dy = toward[1] - origin[1]
    if dx == 0.0 and dy == 0.0:
        return origin
    if isinstance(shape, Rect):
        return _rect_exit(origin, dx, dy, shape)
    return _ellipse_exit(origin, dx, dy, shape)


def clipped_arrow(
    start: _Point,
    end: _Point,
    *,
    start_shape: Shape | None,
    end_shape: Shape | None,
) -> tuple[_Point, _Point] | None:
    """The segment between the rims of both shapes, or ``None`` if it degenerates.

    A missing shape means that end is not clipped. ``None`` comes back in two
    cases: the shapes overlap so far that clipping flipped the segment around,
    or what is left is shorter than `MIN_ARROW`. Both are drawn as *no arrow* —
    a stub, or an arrow pointing backwards, is worse than none at all, and
    neighbouring targets are already legible from their own frames.
    """

    a = ray_exit(start, end, start_shape) if start_shape is not None else start
    b = ray_exit(end, start, end_shape) if end_shape is not None else end

    # Overlapping shapes push both exits past each other, so the clipped segment
    # runs against the original direction — reject it. (Both this and the length
    # test below just return `None`, so their order is immaterial; kept this
    # first only because a reversed segment is the more surprising case.)
    if (b[0] - a[0]) * (end[0] - start[0]) + (b[1] - a[1]) * (end[1] - start[1]) <= 0.0:
        return None
    # Applies even when neither shape clipped anything: a hop shorter than this
    # reads as a dot, not a pointer, so the guide would rather draw nothing.
    if math.hypot(b[0] - a[0], b[1] - a[1]) < MIN_ARROW:
        return None
    return a, b


def _rect_exit(origin: _Point, dx: float, dy: float, r: Rect) -> _Point:
    """Slab method: hit each edge the direction actually heads for, keep the first."""

    ox, oy = origin
    if r.w <= 0.0 or r.h <= 0.0:
        # Redundant with the strict containment below (`r.x < ox < r.x` is never
        # true at zero width), but spelled out so a degenerate box reads as an
        # explicit no-op rather than an accident of the next line.
        return origin
    if not (r.x < ox < r.x + r.w and r.y < oy < r.y + r.h):
        return origin

    hits: list[float] = []
    if dx > 0.0:
        hits.append((r.x + r.w - ox) / dx)
    elif dx < 0.0:
        hits.append((r.x - ox) / dx)
    if dy > 0.0:
        hits.append((r.y + r.h - oy) / dy)
    elif dy < 0.0:
        hits.append((r.y - oy) / dy)
    if not hits:
        return origin

    t = min(hits)
    return (ox + t * dx, oy + t * dy)


def _ellipse_exit(origin: _Point, dx: float, dy: float, e: Ellipse) -> _Point:
    """Quadratic in the space where the ellipse is the unit circle.

    With ``u = (ox - cx) / rx`` and ``v = (oy - cy) / ry`` the rim is
    ``(u + t·du)² + (v + t·dv)² = 1``; inside the ellipse ``u² + v² < 1``, so the
    constant term is negative and exactly one root is positive — that one.
    """

    ox, oy = origin
    if e.rx <= 0.0 or e.ry <= 0.0:
        return origin

    u = (ox - e.cx) / e.rx
    v = (oy - e.cy) / e.ry
    if u * u + v * v >= 1.0:
        return origin

    du = dx / e.rx
    dv = dy / e.ry
    a = du * du + dv * dv
    if a == 0.0:
        # Unreachable via `ray_exit` (it screens out `origin == toward`, and the
        # radii are positive here), but `_ellipse_exit` is module-private and a
        # future caller might not; guard rather than divide by zero below.
        return origin

    b = 2.0 * (u * du + v * dv)
    c = u * u + v * v - 1.0
    t = (-b + math.sqrt(b * b - 4.0 * a * c)) / (2.0 * a)
    return (ox + t * dx, oy + t * dy)
