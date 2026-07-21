from guidebot_recorder.guide.annotate import CLICK_RADIUS, annotations_for

BOX = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}
CENTER = (60.0, 40.0)


def _kinds(anns):
    return [a.kind for a in anns]


def test_click_has_circle_and_arrow_from_prev():
    anns = annotations_for("click", prev_cursor=(5.0, 5.0), center=CENTER, box=BOX)
    assert set(_kinds(anns)) == {"arrow", "click"}
    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x1, arrow.y1, arrow.x2, arrow.y2) == (5.0, 5.0, 60.0, 40.0)
    circle = next(a for a in anns if a.kind == "click")
    assert (circle.cx, circle.cy, circle.r) == (60.0, 40.0, CLICK_RADIUS)


def test_no_arrow_without_prev_cursor():
    anns = annotations_for("click", prev_cursor=None, center=CENTER, box=BOX)
    assert _kinds(anns) == ["click"]


def test_type_makes_typed_frame():
    anns = annotations_for("type", prev_cursor=None, center=CENTER, box=BOX)
    typed = next(a for a in anns if a.kind == "typed")
    assert (typed.x, typed.y, typed.w, typed.h) == (10.0, 20.0, 100.0, 40.0)


def test_hover_makes_glow_rect():
    anns = annotations_for("hover", prev_cursor=None, center=CENTER, box=BOX)
    assert _kinds(anns) == ["hover"]


def test_missing_box_omits_rect_marks():
    anns = annotations_for("type", prev_cursor=None, center=None, box=None)
    assert anns == []
