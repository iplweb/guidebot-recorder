"""Annotation geometry: which marks a step gets, and where the arrow starts and ends."""

from __future__ import annotations

import pytest

from guidebot_recorder.guide.annotate import (
    CLICK_INNER,
    CLICK_OUTER,
    annotations_for,
    target_shape,
)
from guidebot_recorder.guide.geometry import Rect
from guidebot_recorder.models.config import HighlightConfig
from guidebot_recorder.models.scenario import Highlight
from guidebot_recorder.overlay.geometry import Ellipse, ellipse_around, fit_to_bounds

BOX = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}
CENTER = (60.0, 40.0)

#: two boxes on the same row, far enough apart for an arrow to survive clipping
LEFT_BOX = {"x": 0.0, "y": 100.0, "width": 100.0, "height": 40.0}
LEFT_CENTER = (50.0, 120.0)
RIGHT_BOX = {"x": 300.0, "y": 100.0, "width": 100.0, "height": 40.0}
RIGHT_CENTER = (350.0, 120.0)

MARK = Highlight(what="tabela").resolved(HighlightConfig(padding=8, color="#22c55e"))
FRAME = (1280.0, 720.0)


def _kinds(anns):
    return [a.kind for a in anns]


def test_click_frames_the_target_and_stars_the_cursor():
    anns = annotations_for("click", prev_cursor=(5.0, 5.0), center=CENTER, box=BOX)

    assert set(_kinds(anns)) == {"arrow", "frame", "click"}
    star = next(a for a in anns if a.kind == "click")
    assert (star.cx, star.cy) == CENTER
    assert (star.r_inner, star.r_outer) == (CLICK_INNER, CLICK_OUTER)


def test_no_arrow_without_prev_cursor():
    anns = annotations_for("click", prev_cursor=None, center=CENTER, box=BOX)

    assert _kinds(anns) == ["frame", "click"]


@pytest.mark.parametrize("action", ["type", "hover", "select"])
def test_every_targeted_action_frames_its_box(action):
    anns = annotations_for(action, prev_cursor=None, center=CENTER, box=BOX)

    assert _kinds(anns) == ["frame"]
    frame = anns[0]
    assert (frame.x, frame.y, frame.w, frame.h) == (10.0, 20.0, 100.0, 40.0)


def test_missing_box_omits_rect_marks():
    anns = annotations_for("type", prev_cursor=None, center=None, box=None)

    assert anns == []


def test_the_arrow_stops_at_the_frame_of_the_target_not_its_centre():
    anns = annotations_for("click", prev_cursor=LEFT_CENTER, center=RIGHT_CENTER, box=RIGHT_BOX)

    arrow = next(a for a in anns if a.kind == "arrow")
    assert arrow.x2 != RIGHT_CENTER[0]
    assert (arrow.x2, arrow.y2) == pytest.approx((RIGHT_BOX["x"], RIGHT_CENTER[1]))


def test_the_arrow_starts_at_the_edge_of_the_previous_shape():
    anns = annotations_for(
        "click",
        prev_cursor=LEFT_CENTER,
        prev_shape=Rect(x=0.0, y=100.0, w=100.0, h=40.0),
        center=RIGHT_CENTER,
        box=RIGHT_BOX,
    )

    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x1, arrow.y1) == pytest.approx(
        (LEFT_BOX["x"] + LEFT_BOX["width"], LEFT_CENTER[1])
    )


def test_without_a_previous_shape_the_start_is_left_unclipped():
    anns = annotations_for("click", prev_cursor=LEFT_CENTER, center=RIGHT_CENTER, box=RIGHT_BOX)

    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x1, arrow.y1) == LEFT_CENTER


def test_overlapping_targets_get_no_arrow():
    anns = annotations_for(
        "click",
        prev_cursor=(50.0, 120.0),
        prev_shape=Rect(x=0.0, y=100.0, w=100.0, h=40.0),
        center=(60.0, 120.0),
        box=LEFT_BOX,
    )

    assert "arrow" not in _kinds(anns)


@pytest.mark.parametrize("action", ["click", "type", "hover", "select"])
def test_target_shape_is_the_box_of_a_targeted_action(action):
    assert target_shape(action, box=BOX) == Rect(10.0, 20.0, 100.0, 40.0)


def test_target_shape_of_a_highlight_is_its_fitted_ellipse():
    shape = target_shape("highlight", box=BOX, mark=MARK, bounds=FRAME)

    assert shape == fit_to_bounds(ellipse_around(BOX, MARK.padding), width=1280.0, height=720.0)
    assert isinstance(shape, Ellipse)


def test_target_shape_of_a_highlight_without_bounds_is_unfitted():
    assert target_shape("highlight", box=BOX, mark=MARK) == ellipse_around(BOX, MARK.padding)


def test_target_shape_without_a_box_is_none():
    assert target_shape("click", box=None) is None


def test_target_shape_of_a_highlight_without_a_mark_is_none():
    """Bez `mark` padding jest nieznany, więc nie ma z czego policzyć elipsy."""

    assert target_shape("highlight", box=BOX, bounds=FRAME) is None
