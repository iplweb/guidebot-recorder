"""`highlight` w przewodniku PDF: klasyfikacja kroku, adnotacja i rysowanie."""

import pytest

from guidebot_recorder.guide.annotate import annotations_for
from guidebot_recorder.guide.layout import _svg
from guidebot_recorder.guide.model import Annotation
from guidebot_recorder.guide.prolog import classify, scan_for_blockers
from guidebot_recorder.models.config import HighlightConfig
from guidebot_recorder.models.scenario import FlatStep, Highlight, Step
from guidebot_recorder.overlay.geometry import ellipse_around, fit_to_bounds

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


def test_the_arrow_is_clipped_to_the_ellipse_not_to_the_box():
    """Elipsa jest wyższa niż pudełko, więc grot zatrzymuje się nad nim, nie na jego krawędzi."""

    anns = annotations_for(
        "highlight",
        prev_cursor=(10.0, 10.0),
        center=(200.0, 220.0),
        box=BOX,
        mark=MARK,
        bounds=FRAME,
    )

    arrow = next(a for a in anns if a.kind == "arrow")
    ellipse = next(a for a in anns if a.kind == "highlight")
    on_rim = ((arrow.x2 - ellipse.cx) / ellipse.rx) ** 2 + (
        (arrow.y2 - ellipse.cy) / ellipse.ry
    ) ** 2
    assert on_rim == pytest.approx(1.0)
    assert arrow.y2 < BOX["y"]  # nad pudełkiem, więc nie przycięto do prostokąta


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


def test_arrow_runs_to_the_box_centre_when_it_falls_outside_the_fitted_ellipse():
    """Świadomie zaakceptowany przypadek brzegowy, nie ideał.

    Gdy `fit_to_bounds` zepchnie elipsę tak, że środek pudełka celu wypada POZA
    nią (mały cel w rogu kadru), `ray_exit` zwraca `origin` — „nie jestem
    wewnątrz" — więc grot NIE jest przycinany i strzałka dobiega aż do środka
    pudełka, przebijając elipsę. To obecne, celowo przypięte zachowanie: test
    dokumentuje ten kompromis, żeby jego zmiana była świadoma, nie przypadkowa.
    """

    corner_box = {"x": 0.0, "y": 0.0, "width": 20.0, "height": 20.0}
    box_center = (10.0, 10.0)  # = środek `corner_box`

    # Elipsa zostaje odepchnięta od rogu przez margines `fit_to_bounds`, aż środek
    # pudełka ląduje poza nią — potwierdzamy to wprost równaniem elipsy (> 1).
    fitted = fit_to_bounds(
        ellipse_around(corner_box, MARK.padding), width=FRAME[0], height=FRAME[1]
    )
    u = (box_center[0] - fitted.cx) / fitted.rx
    v = (box_center[1] - fitted.cy) / fitted.ry
    assert u * u + v * v > 1.0  # środek pudełka naprawdę leży poza dopasowaną elipsą

    anns = annotations_for(
        "highlight",
        prev_cursor=(600.0, 400.0),
        center=box_center,
        box=corner_box,
        mark=MARK,
        bounds=FRAME,
    )

    arrow = next(a for a in anns if a.kind == "arrow")
    assert (arrow.x2, arrow.y2) == pytest.approx(box_center)  # brak przycięcia do elipsy


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
