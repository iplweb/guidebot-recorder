from pathlib import Path

from guidebot_recorder.guide.layout import render_html
from guidebot_recorder.guide.model import Annotation, GuidePage


def test_one_section_per_page_and_title():
    pages = [
        GuidePage(kind="slide", screenshot=None, text="", heading="Sekcja 1", annotations=[]),
        GuidePage(
            kind="step",
            screenshot=Path("/tmp/shot.png"),
            text="Kliknij tu",
            heading=None,
            annotations=[Annotation(kind="click", cx=60.0, cy=40.0, r_inner=16.0, r_outer=30.0)],
            screenshot_size=(800, 600),
        ),
    ]
    html = render_html(pages, title="Mój przewodnik")
    assert html.count('class="page"') == 2
    assert "Mój przewodnik" in html


def _shot_page(annotations):
    return GuidePage(
        kind="step",
        screenshot=Path("/tmp/shot.png"),
        text="t",
        heading=None,
        annotations=annotations,
        screenshot_size=(800, 600),
    )


def test_screenshot_page_has_svg_viewbox_and_star():
    pages = [_shot_page([Annotation(kind="click", cx=100.0, cy=200.0, r_inner=16.0, r_outer=30.0)])]
    html = render_html(pages, title="x")
    assert 'viewBox="0 0 800 600"' in html
    assert html.count('<line class="star"') == 8
    assert '<circle class="circle"' not in html


def test_click_star_arms_run_from_inner_to_outer_radius():
    pages = [_shot_page([Annotation(kind="click", cx=100.0, cy=200.0, r_inner=16.0, r_outer=30.0)])]
    html = render_html(pages, title="x")
    # Ramię o kącie 0° biegnie poziomo w prawo, od cx+16 do cx+30.
    assert '<line class="star" x1="116.0" y1="200.0" x2="130.0" y2="200.0"/>' in html
    # Ramię o kącie 45° — obie współrzędne przesunięte o r/sqrt(2), zaokrąglone do 2 miejsc.
    assert '<line class="star" x1="111.31" y1="211.31" x2="121.21" y2="221.21"/>' in html


def test_frame_annotation_renders_rounded_rect():
    pages = [_shot_page([Annotation(kind="frame", x=10.0, y=20.0, w=300.0, h=40.0)])]
    html = render_html(pages, title="x")
    assert '<rect class="frame" x="10.0" y="20.0" width="300.0" height="40.0" rx="4"/>' in html
    assert '<rect class="rect"' not in html


def test_stylesheet_defines_the_star_and_frame_rules():
    # A `<line>`/`<rect>` with no `stroke` is invisible: the arms and frames get
    # their red stroke only from the `.star` / `.frame` CSS rules. Rename either
    # class in the stylesheet and every PDF silently loses its stars or frames,
    # with the whole suite still green — so pin the rules by name here.
    html = render_html([_shot_page([])], title="x")
    assert ".star {" in html
    assert ".frame {" in html


def test_text_page_has_no_svg():
    pages = [
        GuidePage(kind="text", screenshot=None, text="tylko tekst", heading=None, annotations=[])
    ]
    html = render_html(pages, title="x")
    assert "<svg" not in html
    assert "tylko tekst" in html
