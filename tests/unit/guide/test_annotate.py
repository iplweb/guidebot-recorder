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


ROW = {"x": 12.0, "y": 70.0, "width": 96.0, "height": 26.0}
ROW_CENTER = (60.0, 83.0)


def test_select_without_a_row_keeps_the_marks_it_always_had():
    """`mode: native` unfurls nothing, so there is no row to send anyone to."""

    anns = annotations_for("select", prev_cursor=(5.0, 5.0), center=CENTER, box=BOX)
    assert set(_kinds(anns)) == {"arrow", "selected"}
    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x2, arrow.y2) == CENTER
    rect = next(a for a in anns if a.kind == "selected")
    assert (rect.x, rect.y, rect.w, rect.h) == (10.0, 20.0, 100.0, 40.0)


def test_select_with_an_open_list_circles_the_row_and_frames_the_control():
    anns = annotations_for(
        "select", prev_cursor=(5.0, 5.0), center=CENTER, box=BOX, row_box=ROW, row_center=ROW_CENTER
    )
    assert set(_kinds(anns)) == {"arrow", "selected", "click"}
    # the circle marks the option about to be clicked...
    circle = next(a for a in anns if a.kind == "click")
    assert (circle.cx, circle.cy) == ROW_CENTER
    # ...the rectangle stays on the control, so the field is still legible...
    rect = next(a for a in anns if a.kind == "selected")
    assert (rect.x, rect.y, rect.w, rect.h) == (10.0, 20.0, 100.0, 40.0)
    # ...and the arrow ends on the row, not on the control
    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x2, arrow.y2) == ROW_CENTER


def test_the_row_circle_never_spills_onto_the_neighbouring_options():
    """A fixed radius would draw one mark across three rows — i.e. point at three
    options at once, which is worse than not marking anything."""

    anns = annotations_for(
        "select", prev_cursor=None, center=CENTER, box=BOX, row_box=ROW, row_center=ROW_CENTER
    )
    circle = next(a for a in anns if a.kind == "click")
    assert circle.r == ROW["height"] / 2
    assert circle.cy - circle.r >= ROW["y"]
    assert circle.cy + circle.r <= ROW["y"] + ROW["height"]


def test_a_boxless_control_still_marks_the_row():
    """A `display: none` original has no box; the option row is what matters."""

    anns = annotations_for(
        "select", prev_cursor=None, center=None, box=None, row_box=ROW, row_center=ROW_CENTER
    )
    assert _kinds(anns) == ["click"]
