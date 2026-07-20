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
            annotations=[Annotation(kind="click", cx=60.0, cy=40.0, r=22.0)],
            screenshot_size=(800, 600),
        ),
    ]
    html = render_html(pages, title="Mój przewodnik")
    assert html.count('class="page"') == 2
    assert "Mój przewodnik" in html


def test_screenshot_page_has_svg_viewbox_and_circle():
    pages = [
        GuidePage(
            kind="step",
            screenshot=Path("/tmp/shot.png"),
            text="t",
            heading=None,
            annotations=[Annotation(kind="click", cx=1.0, cy=2.0, r=22.0)],
            screenshot_size=(800, 600),
        )
    ]
    html = render_html(pages, title="x")
    assert 'viewBox="0 0 800 600"' in html
    assert "<circle" in html


def test_text_page_has_no_svg():
    pages = [
        GuidePage(kind="text", screenshot=None, text="tylko tekst", heading=None, annotations=[])
    ]
    html = render_html(pages, title="x")
    assert "<svg" not in html
    assert "tylko tekst" in html
