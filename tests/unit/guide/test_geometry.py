"""Arrow-clipping geometry — pure math, no browser, no I/O."""

from __future__ import annotations

import pytest

from guidebot_recorder.guide.geometry import (
    MIN_ARROW,
    Rect,
    clipped_arrow,
    ray_exit,
    rect_from_box,
)
from guidebot_recorder.overlay.geometry import Ellipse

RECT = Rect(x=0.0, y=0.0, w=100.0, h=50.0)
CENTER = (50.0, 25.0)


def _on_ellipse(point: tuple[float, float], e: Ellipse) -> float:
    """Left-hand side of the ellipse equation — 1.0 exactly on the rim."""

    return ((point[0] - e.cx) / e.rx) ** 2 + ((point[1] - e.cy) / e.ry) ** 2


def test_rect_from_box_reads_playwrights_bounding_box():
    assert rect_from_box({"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}) == Rect(
        10.0, 20.0, 100.0, 40.0
    )


@pytest.mark.parametrize(
    ("toward", "expected"),
    [
        ((500.0, 25.0), (100.0, 25.0)),  # right edge
        ((-500.0, 25.0), (0.0, 25.0)),  # left edge
        ((50.0, 500.0), (50.0, 50.0)),  # bottom edge
        ((50.0, -500.0), (50.0, 0.0)),  # top edge
    ],
)
def test_ray_leaves_a_rect_through_the_edge_it_points_at(toward, expected):
    assert ray_exit(CENTER, toward, RECT) == pytest.approx(expected)


def test_a_diagonal_ray_leaves_through_the_nearer_edge():
    # direction (1, 1) from the centre: the box is wider than it is tall, so the
    # horizontal slab is hit at t=0.5 and the vertical one already at t=0.25.
    assert ray_exit(CENTER, (150.0, 125.0), RECT) == pytest.approx((75.0, 50.0))


def test_a_ray_leaves_an_ellipse_on_its_rim():
    e = Ellipse(cx=100.0, cy=100.0, rx=50.0, ry=20.0)

    exit_point = ray_exit((100.0, 100.0), (200.0, 150.0), e)

    assert _on_ellipse(exit_point, e) == pytest.approx(1.0)


def test_the_ellipse_exit_lies_on_the_ray_not_behind_it():
    e = Ellipse(cx=0.0, cy=0.0, rx=40.0, ry=40.0)

    exit_point = ray_exit((0.0, 0.0), (-10.0, 0.0), e)

    assert exit_point == pytest.approx((-40.0, 0.0))


@pytest.mark.parametrize(
    "shape",
    [RECT, Ellipse(cx=50.0, cy=25.0, rx=50.0, ry=25.0)],
    ids=["rect", "ellipse"],
)
def test_a_point_outside_the_shape_is_returned_unchanged(shape):
    outside = (500.0, 500.0)

    assert ray_exit(outside, (0.0, 0.0), shape) == outside


@pytest.mark.parametrize(
    "shape",
    [RECT, Ellipse(cx=50.0, cy=25.0, rx=50.0, ry=25.0)],
    ids=["rect", "ellipse"],
)
def test_a_ray_with_no_direction_is_returned_unchanged(shape):
    assert ray_exit(CENTER, CENTER, shape) == CENTER


@pytest.mark.parametrize(
    "shape",
    [
        Rect(x=0.0, y=25.0, w=100.0, h=0.0),
        Rect(x=50.0, y=0.0, w=0.0, h=50.0),
        Ellipse(cx=50.0, cy=25.0, rx=0.0, ry=25.0),
        Ellipse(cx=50.0, cy=25.0, rx=50.0, ry=0.0),
    ],
    ids=["flat-rect", "thin-rect", "flat-ellipse", "thin-ellipse"],
)
def test_a_degenerate_shape_is_returned_unchanged(shape):
    assert ray_exit(CENTER, (500.0, 25.0), shape) == CENTER


def test_the_arrow_between_two_disjoint_rects_runs_edge_to_edge():
    left = Rect(x=0.0, y=0.0, w=100.0, h=50.0)
    right = Rect(x=200.0, y=0.0, w=100.0, h=50.0)

    segment = clipped_arrow((50.0, 25.0), (250.0, 25.0), start_shape=left, end_shape=right)

    assert segment is not None
    a, b = segment
    assert a == pytest.approx((100.0, 25.0))  # right edge of the left box
    assert b == pytest.approx((200.0, 25.0))  # left edge of the right box
    assert b[0] > a[0]  # direction preserved


def test_overlapping_targets_get_no_arrow():
    a_box = Rect(x=0.0, y=0.0, w=100.0, h=100.0)
    b_box = Rect(x=50.0, y=0.0, w=100.0, h=100.0)

    assert clipped_arrow((50.0, 50.0), (100.0, 50.0), start_shape=a_box, end_shape=b_box) is None


def test_targets_closer_than_min_arrow_get_no_arrow():
    left = Rect(x=0.0, y=0.0, w=100.0, h=50.0)
    right = Rect(x=108.0, y=0.0, w=100.0, h=50.0)  # an 8 px gap, below MIN_ARROW

    assert clipped_arrow((50.0, 25.0), (158.0, 25.0), start_shape=left, end_shape=right) is None


def test_without_shapes_a_long_segment_survives_untouched():
    segment = clipped_arrow((0.0, 0.0), (100.0, 0.0), start_shape=None, end_shape=None)

    assert segment == ((0.0, 0.0), (100.0, 0.0))


def test_without_shapes_a_short_segment_is_still_dropped():
    short = MIN_ARROW - 2.0

    assert clipped_arrow((0.0, 0.0), (short, 0.0), start_shape=None, end_shape=None) is None


def test_only_one_shape_clips_only_that_end():
    segment = clipped_arrow((50.0, 25.0), (400.0, 25.0), start_shape=RECT, end_shape=None)

    assert segment == (pytest.approx((100.0, 25.0)), (400.0, 25.0))
