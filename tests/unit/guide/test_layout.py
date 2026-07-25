from pathlib import Path

from guidebot_recorder.guide.layout import fold_narration, render_html
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


def test_no_frame_is_ever_drawn_over_the_still():
    """Ramka celu nie może powstać w SVG — zawsze przykryłaby kursor.

    Maluje ją strona przed migawką (`stills._framed_screenshot`), więc w PDF-ie
    nie ma po niej ani reguły CSS, ani prostokąta. Ten test jest zamkiem na
    powrót: gdyby ktoś „przywrócił" rysowanie ramki w `_svg`, kursor znowu
    wylądowałby pod czerwonym wypełnieniem.
    """

    html = render_html([_shot_page([])], title="x")
    assert '<rect class="frame"' not in html
    assert ".frame {" not in html


def test_stylesheet_defines_the_star_rule():
    # A `<line>` with no `stroke` is invisible: the star's arms get their red
    # stroke only from the `.star` CSS rule. Rename the class in the stylesheet
    # and every PDF silently loses its stars with the whole suite still green —
    # so pin the rule by name here.
    html = render_html([_shot_page([])], title="x")
    assert ".star {" in html


def test_a_select_steps_marks_render_through_the_one_shared_arrowhead():
    """A `select:` page inherits the shrunken arrowhead instead of its own copy.

    The head is defined once, in `_ARROW_MARKER`, and every `.arrow` picks it up
    from a single `marker-end` rule — so shrinking it (markerWidth 10 -> 6, i.e.
    ~40 px down to ~24 once `stroke-width: 4` scales it) reached the select
    step's arrow to the option row for free. Pinned so a later select-specific
    marker, or a regrown one, is caught: the stub it was drawing is worst exactly
    on the short row-to-row hops a select produces.
    """

    pages = [
        _shot_page(
            [
                Annotation(kind="arrow", x1=10.0, y1=10.0, x2=200.0, y2=300.0),
                Annotation(kind="click", cx=100.0, cy=200.0, r_inner=16.0, r_outer=30.0),
            ]
        )
    ]
    html = render_html(pages, title="x")

    assert html.count("<marker") == 1  # one definition, not one per action
    assert 'markerWidth="6"' in html
    assert "marker-end: url(#ah);" in html
    assert '<line class="arrow" x1="10.0" y1="10.0" x2="200.0" y2="300.0"/>' in html


def test_main_window_page_is_not_letterboxed():
    """Zrzut wypełniający płótno renderuje się dokładnie jak przed zmianą.

    Letterboxing włącza się wyłącznie przy różnicy rozmiarów, więc każda strona
    okna głównego musi zostać bez stylów inline — inaczej zmiana „dla popupów"
    po cichu przesunęłaby wszystkie dotychczasowe strony.
    """

    page = GuidePage(
        kind="step",
        screenshot=Path("/tmp/shot.png"),
        text="t",
        heading=None,
        annotations=[],
        screenshot_size=(1376, 800),
        canvas_size=(1376, 800),
    )
    html = render_html([page], title="x")
    assert 'class="shot"' in html
    assert "aspect-ratio" not in html
    assert 'class="plate"' in html


def test_popup_page_is_centred_on_the_main_window_canvas():
    """Mniejszy zrzut dostaje płótno o proporcjach okna głównego i własną skalę.

    600/1376 ≈ 43.605%, 700/800 = 87.5% — obie liczby muszą wyjść z jednego
    stosunku, bo inaczej obrazek zostałby rozciągnięty względem sąsiednich stron.
    """

    page = GuidePage(
        kind="step",
        screenshot=Path("/tmp/popup.png"),
        text="t",
        heading=None,
        annotations=[],
        screenshot_size=(600, 700),
        canvas_size=(1376, 800),
    )
    html = render_html([page], title="x")
    assert 'style="aspect-ratio:1376/800"' in html
    assert 'style="width:43.605%;height:87.5%"' in html


def test_annotations_stay_in_the_stills_own_coordinate_system():
    """Adnotacje popupu opisują piksele zrzutu, nie płótna.

    To jest powód, dla którego `img` i `svg` siedzą we wspólnym `.plate`, a nie
    w `.shot`: viewBox musi zostać rozmiarem zrzutu (600×700), a warstwa SVG musi
    leżeć na obrazku, nie na całym płótnie. Gdyby SVG został przypięty do `.shot`,
    każda strzałka i gwiazdka przesunęłaby się o szerokość marginesu.
    """

    page = GuidePage(
        kind="step",
        screenshot=Path("/tmp/popup.png"),
        text="t",
        heading=None,
        annotations=[Annotation(kind="click", cx=100.0, cy=200.0, r_inner=16.0, r_outer=30.0)],
        screenshot_size=(600, 700),
        canvas_size=(1376, 800),
    )
    html = render_html([page], title="x")
    assert 'viewBox="0 0 600 700"' in html
    assert ".plate svg {" in html
    assert ".shot svg {" not in html


def _text(text):
    return GuidePage(kind="text", screenshot=None, text=text, heading=None, annotations=[])


def _slide(heading):
    return GuidePage(kind="slide", screenshot=None, text="", heading=heading, annotations=[])


def test_narration_after_a_picture_folds_onto_it_under_a_rule():
    """`say:` między akcjami opisuje to, na co czytelnik patrzy.

    Nie zasłużyło na osobną kartkę z jednym zdaniem na środku — ląduje w panelu
    bocznym poprzedniego kadru, oddzielone poziomą kreską.
    """

    html = render_html(
        fold_narration([_shot_page([]), _text("Okno otwiera się osobno.")]), title="x"
    )
    assert html.count('class="page"') == 1
    assert '<hr class="fold"/>' in html
    assert "Okno otwiera się osobno." in html


def test_narration_with_no_picture_before_it_keeps_its_own_page():
    """Nie ma czego dosiąść — zostaje pełnowymiarowa strona tekstowa."""

    html = render_html(fold_narration([_text("Zanim zaczniemy.")]), title="x")
    assert html.count('class="page"') == 1
    assert '<hr class="fold"/>' not in html
    assert "Zanim zaczniemy." in html


def test_a_slide_neither_absorbs_narration_nor_is_absorbed():
    """Slajd to celowa plansza pełnoekranowa, więc nie zwija się z niczym.

    Slajd tytułowy z narracją zaraz za nim dawałby inaczej stronę tytułową
    z doklejonym zdaniem — a to jest przerywnik, nie kadr do opisania.
    """

    html = render_html(fold_narration([_slide("Logowanie"), _text("Witaj.")]), title="x")
    assert html.count('class="page"') == 2
    assert '<hr class="fold"/>' not in html


def test_several_narrations_fold_onto_one_picture_each_under_its_own_rule():
    pages = [_shot_page([]), _text("Pierwsze zdanie."), _text("Drugie zdanie.")]
    html = render_html(fold_narration(pages), title="x")
    assert html.count('class="page"') == 1
    assert html.count('<hr class="fold"/>') == 2


def test_empty_narration_folds_to_nothing_rather_than_a_bare_rule():
    """Pusta narracja nie ma czego wnieść, a kreska bez tekstu to śmieć na stronie.

    To zarazem koniec pustych kartek: krok narracyjny bez tekstu drukował dotąd
    stronę z niczym.
    """

    html = render_html(fold_narration([_shot_page([]), _text("")]), title="x")
    assert html.count('class="page"') == 1
    assert '<hr class="fold"/>' not in html


def test_fold_narration_is_what_the_document_is_counted_by():
    """Zwinięta lista to jedna i ta sama lista, którą się drukuje i liczy.

    `run_guide` zwraca długość listy jako liczbę stron, więc gdy zwijanie siedziało
    wewnątrz `render_html`, CLI ogłaszało osiem stron pięciostronicowego PDF-a.
    Ten test pilnuje, że zwijanie jest krokiem wywołującego, a nie efektem ubocznym
    renderowania.
    """

    pages = [_slide("Tytuł"), _shot_page([]), _text("dopisek"), _text("i jeszcze")]
    sheets = fold_narration(pages)
    html = render_html(sheets, title="x")

    assert len(sheets) == 2
    assert html.count('class="page"') == len(sheets)
    # A gdyby renderer nadal zwijał sam, podwójne zwinięcie byłoby niewykrywalne.
    assert render_html(pages, title="x").count('class="page"') == 4


def test_folding_does_not_mutate_the_caller_s_pages():
    """`render_html` nie może przepisywać wejścia — to lista wywołującego."""

    shot, text = _shot_page([]), _text("dopisek")
    fold_narration([shot, text])
    assert shot.folded_text == []


def test_text_page_has_no_svg():
    pages = [
        GuidePage(kind="text", screenshot=None, text="tylko tekst", heading=None, annotations=[])
    ]
    html = render_html(pages, title="x")
    assert "<svg" not in html
    assert "tylko tekst" in html
