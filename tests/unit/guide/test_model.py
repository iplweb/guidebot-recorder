from guidebot_recorder.guide.model import Annotation, GuidePage, page_text
from guidebot_recorder.models.scenario import Step


def test_page_text_prefers_caption():
    step = Step(click="btn", say="krótko", caption="dłuższy opis do PDF")
    assert page_text(step) == "dłuższy opis do PDF"


def test_page_text_falls_back_to_narration():
    assert page_text(Step(teach="wpisz login")) == "wpisz login"


def test_page_text_empty_when_no_text():
    assert page_text(Step(navigate="https://example.com")) == ""


def test_annotation_and_page_construct():
    a = Annotation(kind="click", cx=1.0, cy=2.0, r=18.0)
    page = GuidePage(
        kind="step", screenshot=None, text="t", heading=None,
        annotations=[a], screenshot_size=(800, 600),
    )
    assert page.annotations[0].kind == "click"
    assert page.screenshot_size == (800, 600)
