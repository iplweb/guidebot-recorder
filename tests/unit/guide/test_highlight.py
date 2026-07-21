"""`highlight` w przewodniku PDF: klasyfikacja kroku, adnotacja i rysowanie."""

import pytest

from guidebot_recorder.guide.annotate import annotations_for
from guidebot_recorder.guide.layout import _svg
from guidebot_recorder.guide.model import Annotation
from guidebot_recorder.guide.prolog import classify, scan_for_blockers
from guidebot_recorder.models.config import HighlightConfig
from guidebot_recorder.models.scenario import FlatStep, Highlight, Step

BOX = {"x": 100.0, "y": 200.0, "width": 200.0, "height": 40.0}
MARK = Highlight(what="tabela").resolved(HighlightConfig(padding=8, color="#22c55e"))
FRAME = (1280.0, 720.0)


def _flat(step: Step) -> FlatStep:
    return FlatStep(step=step, branch=None, is_gate=False)


def test_a_highlight_step_gets_a_screenshot_page():
    assert classify(_flat(Step.model_validate({"highlight": "tabela"}))) == "action"


def test_preflight_accepts_a_highlight_step():
    """Bez wpisu na liście obsługiwanych komend preflight zatrzymałby scenariusz."""

    scan_for_blockers([_flat(Step.model_validate({"highlight": "tabela"}))], [None])


def test_annotation_is_an_ellipse_around_the_target():
    anns = annotations_for(
        "highlight", prev_cursor=None, center=(200.0, 220.0), box=BOX, mark=MARK, bounds=FRAME
    )

    assert [a.kind for a in anns] == ["highlight"]
    ellipse = anns[0]
    assert (ellipse.cx, ellipse.cy) == (200.0, 220.0)
    assert ellipse.rx > BOX["width"] / 2  # opisana na prostokącie, więc szersza
    assert ellipse.color == "#22c55e"


def test_annotation_keeps_the_usual_arrow_from_the_previous_cursor():
    anns = annotations_for(
        "highlight",
        prev_cursor=(10.0, 10.0),
        center=(200.0, 220.0),
        box=BOX,
        mark=MARK,
        bounds=FRAME,
    )

    assert [a.kind for a in anns] == ["arrow", "highlight"]


def test_no_geometry_means_no_marks_at_all():
    anns = annotations_for(
        "highlight", prev_cursor=(10.0, 10.0), center=None, box=None, mark=MARK, bounds=FRAME
    )

    assert anns == []


def test_ellipse_is_kept_inside_the_screenshot():
    wide = {"x": 10.0, "y": 300.0, "width": 1260.0, "height": 40.0}

    anns = annotations_for(
        "highlight", prev_cursor=None, center=(640.0, 320.0), box=wide, mark=MARK, bounds=FRAME
    )

    ellipse = anns[0]
    assert ellipse.cx - ellipse.rx >= 0.0
    assert ellipse.cx + ellipse.rx <= FRAME[0]


def test_svg_draws_the_ellipse_in_its_own_colour():
    svg = _svg(
        [Annotation(kind="highlight", cx=200, cy=220, rx=150, ry=40, color="#22c55e")], FRAME
    )

    assert "<ellipse" in svg
    assert 'stroke="#22c55e"' in svg


@pytest.mark.parametrize("hostile", ['"><script>alert(1)</script>', '#fff" onload="x'])
def test_svg_escapes_a_hostile_colour(hostile):
    svg = _svg([Annotation(kind="highlight", cx=1, cy=1, rx=2, ry=2, color=hostile)], FRAME)

    assert "<script>" not in svg
    assert 'onload="' not in svg
