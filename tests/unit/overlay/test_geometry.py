"""Geometria elipsy zakreślającej — czysta matematyka, bez przeglądarki."""

from __future__ import annotations

import math

import pytest

from guidebot_recorder.overlay.geometry import (
    Ellipse,
    ellipse_around,
    ellipse_perimeter,
    fit_to_bounds,
)


def _covers(e: Ellipse, box: dict) -> bool:
    """Czy elipsa zawiera wszystkie cztery rogi prostokąta?"""

    corners = (
        (box["x"], box["y"]),
        (box["x"] + box["width"], box["y"]),
        (box["x"], box["y"] + box["height"]),
        (box["x"] + box["width"], box["y"] + box["height"]),
    )
    return all(
        ((px - e.cx) / e.rx) ** 2 + ((py - e.cy) / e.ry) ** 2 <= 1 + 1e-9 for px, py in corners
    )


def test_ellipse_contains_every_corner_of_the_box():
    box = {"x": 100.0, "y": 50.0, "width": 240.0, "height": 60.0}

    assert _covers(ellipse_around(box, padding=0.0), box)


def test_ellipse_is_centred_on_the_box():
    e = ellipse_around({"x": 100.0, "y": 50.0, "width": 240.0, "height": 60.0}, padding=0.0)

    assert (e.cx, e.cy) == (220.0, 80.0)


def test_padding_grows_both_radii():
    box = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 40.0}
    tight = ellipse_around(box, padding=0.0)
    loose = ellipse_around(box, padding=10.0)

    assert loose.rx > tight.rx
    assert loose.ry > tight.ry


def test_square_target_gives_a_circle():
    e = ellipse_around({"x": 0.0, "y": 0.0, "width": 80.0, "height": 80.0}, padding=6.0)

    assert e.rx == pytest.approx(e.ry)


def test_fit_to_bounds_clamps_radii_to_the_frame():
    wide = Ellipse(cx=640.0, cy=360.0, rx=900.0, ry=80.0)

    fitted = fit_to_bounds(wide, width=1280.0, height=720.0, margin=4.0)

    assert fitted.rx == pytest.approx(636.0)  # 1280/2 - 4
    assert fitted.ry == pytest.approx(80.0)  # mieści się, zostaje bez zmian


def test_fit_to_bounds_shifts_the_centre_so_the_ellipse_stays_inside():
    near_edge = Ellipse(cx=30.0, cy=360.0, rx=100.0, ry=50.0)

    fitted = fit_to_bounds(near_edge, width=1280.0, height=720.0, margin=4.0)

    assert fitted.cx - fitted.rx >= 4.0 - 1e-9
    assert fitted.rx == pytest.approx(100.0)  # promień nietknięty, przesunął się środek


def test_fit_to_bounds_survives_a_target_larger_than_the_frame():
    huge = Ellipse(cx=640.0, cy=360.0, rx=4000.0, ry=3000.0)

    fitted = fit_to_bounds(huge, width=1280.0, height=720.0, margin=4.0)

    assert fitted.rx == pytest.approx(636.0)
    assert fitted.ry == pytest.approx(356.0)
    assert fitted.cx == pytest.approx(640.0)
    assert fitted.cy == pytest.approx(360.0)


def test_perimeter_of_a_circle_matches_two_pi_r():
    circle = Ellipse(cx=0.0, cy=0.0, rx=50.0, ry=50.0)

    assert ellipse_perimeter(circle) == pytest.approx(2 * math.pi * 50.0, rel=1e-6)


def test_perimeter_grows_with_the_longer_axis():
    small = ellipse_perimeter(Ellipse(0.0, 0.0, 50.0, 50.0))
    stretched = ellipse_perimeter(Ellipse(0.0, 0.0, 400.0, 50.0))

    assert stretched > small
