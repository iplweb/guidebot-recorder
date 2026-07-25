"""Popup windows in the PDF guide: the active window switches, and switches back.

Driven with fakes, no browser. The fakes here stand in for the one Playwright API
the feature leans on — ``BrowserContext.expect_page`` — and stay in this file
rather than travelling to ``_capture_helpers.py`` because nothing else uses them.

What is actually being pinned is an *ordering*: the step that opens a popup is
photographed in the window the reader clicked in, and only the steps after it are
read against the popup. Get that backwards and every guide with a login flow
shows the reader a picture of the wrong window at the moment they act.
"""

from __future__ import annotations

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.prolog import GuideError
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Step

from ._capture_helpers import (
    FakeRecorder,
    _async_none,
    _cfg,
    _compiled,
    _fp,
    _target,
)


class _Opened:
    """Stand-in for the object ``expect_page()`` yields.

    ``None`` for the popup means the event never fired, and the refusal is raised
    from ``__aexit__`` — which is where Playwright raises it too, so the
    production code's ``try`` has to span the whole ``async with``, not just the
    ``await opened.value`` after it.
    """

    def __init__(self, popup):
        self._popup = popup

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        if self._popup is None:
            raise PlaywrightTimeoutError('Timeout exceeded while waiting for event "page"')
        return False

    @property
    def value(self):
        return self._resolve()

    async def _resolve(self):
        return self._popup


class FakeContext:
    def __init__(self, popup):
        self.popup = popup
        self.expect_timeouts: list[float | None] = []

    def expect_page(self, timeout=None):
        self.expect_timeouts.append(timeout)
        return _Opened(self.popup)


class NamedPage:
    """A page that says which window a screenshot came from."""

    def __init__(self, name: str, events: list[str], size=(1280, 720), context=None):
        self.name = name
        self.events = events
        self.viewport_size = {"width": size[0], "height": size[1]}
        self.context = context
        self.closed = False

    async def screenshot(self, path):
        from pathlib import Path

        Path(path).write_bytes(b"fake")
        self.events.append(f"shot:{self.name}")

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self.events.append(f"close:{self.name}")

    async def wait_for_load_state(self, _state):
        return None


def _click(opens_popup=False):
    return CachedAction(
        action="click",
        target=_target(),
        expect="none",
        opens_popup=opens_popup,
        fingerprint=_fp(command_kind="click"),
    )


def _windows(events, popup_size=(600, 700)):
    """Main window plus the popup its context will hand over."""

    popup = NamedPage("popup", events, size=popup_size)
    main = NamedPage("main", events, context=FakeContext(popup))
    popup.context = main.context
    return main, popup


async def test_popup_click_switches_the_active_window_and_close_switches_back(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, popup = _windows(events)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="ikona profilu"),
            Step(click="pole w wyskakującym oknie"),
            Step(close_window=True, say="wracamy do serwisu"),
            Step(click="menu w oknie głównym"),
        ],
    )
    popup_recorders: list[FakeRecorder] = []

    def make_recorder(page):
        assert page is popup
        recorder = FakeRecorder(events)
        popup_recorders.append(recorder)
        return recorder

    await capture_pages(
        scenario,
        _compiled([_click(opens_popup=True), _click(), None, _click()]),
        main,
        FakeRecorder(events),
        tmp_path / "shots",
        timeout=15.0,
        make_recorder=make_recorder,
    )

    # The click that opens the popup is photographed in the MAIN window: that is
    # what the reader is looking at when they perform it.
    assert [e for e in events if e.startswith(("shot:", "close:"))] == [
        "shot:main",
        "shot:popup",
        "close:popup",
        "shot:main",
    ]
    assert len(popup_recorders) == 1
    assert popup.closed is True


async def test_popup_page_keeps_its_own_size_and_the_main_windows_canvas(tmp_path, monkeypatch):
    """Zrzut popupu zostaje w swoim rozmiarze; płótno pozostaje okna głównego.

    Te dwie liczby są jedynym sygnałem „to jest popup" — `layout._letterbox`
    włącza się dokładnie na ich różnicy, więc gdyby capture zapisał rozmiar
    płótna jako rozmiar zrzutu, letterboxing zniknąłby bez żadnego czerwonego
    testu w warstwie układu.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, popup = _windows(events)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(click="ikona profilu"), Step(click="pole w wyskakującym oknie")],
    )
    pages = await capture_pages(
        scenario,
        _compiled([_click(opens_popup=True), _click()]),
        main,
        FakeRecorder(events),
        tmp_path / "shots",
        timeout=15.0,
        make_recorder=lambda page: FakeRecorder(events),
    )

    assert pages[0].screenshot_size == (1280, 720)
    assert pages[1].screenshot_size == (600, 700)
    # `_cfg()` viewport — the canvas never follows the active window.
    assert [p.canvas_size for p in pages] == [(1280, 720), (1280, 720)]


async def test_first_popup_step_draws_no_arrow_from_the_other_window(tmp_path, monkeypatch):
    """Ślad kursora zeruje się przy zmianie okna.

    Strzałka prowadzi wzrok od poprzedniego celu do bieżącego. Poprzedni cel
    został w innym oknie i na innym obrazku, więc narysowana tu strzałka
    wskazywałaby współrzędne, które na tej stronie nie znaczą nic.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, popup = _windows(events)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(click="ikona profilu"), Step(click="pole w wyskakującym oknie")],
    )
    pages = await capture_pages(
        scenario,
        _compiled([_click(opens_popup=True), _click()]),
        main,
        FakeRecorder(events),
        tmp_path / "shots",
        timeout=15.0,
        make_recorder=lambda page: FakeRecorder(events),
    )

    assert not [a for a in pages[1].annotations if a.kind == "arrow"]


async def test_a_popup_that_never_opens_fails_loudly_with_the_yaml_location(tmp_path, monkeypatch):
    """Brak okna mimo zamrożonej flagi to twardy błąd, nie krok do pominięcia.

    Cicha tolerancja dałaby PDF, który wygląda na komplet instrukcji, a gubi
    wszystko, co dzieje się w wyskakującym oknie — czyli zwykle sedno scenariusza.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, _popup = _windows(events)
    main.context.popup = None  # click opens nothing
    scenario = Scenario(config=_cfg(), steps=[Step(click="ikona profilu")])

    with pytest.raises(GuideError, match="nowe okno"):
        await capture_pages(
            scenario,
            _compiled([_click(opens_popup=True)]),
            main,
            FakeRecorder(events),
            tmp_path / "shots",
            timeout=15.0,
            make_recorder=lambda page: FakeRecorder(events),
        )


async def test_entering_a_popup_without_a_recorder_factory_blames_the_caller(tmp_path, monkeypatch):
    """Brak fabryki to błąd wywołania `capture_pages`, nie wada scenariusza.

    Dlatego `RuntimeError`, a nie `GuideError`: `GuideError` niesie `plik:linia`
    i mówi autorowi, co poprawić w YAML-u — a tu nie ma czego poprawiać.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, _popup = _windows(events)
    scenario = Scenario(config=_cfg(), steps=[Step(click="ikona profilu")])

    with pytest.raises(RuntimeError, match="fabryki Recordera"):
        await capture_pages(
            scenario,
            _compiled([_click(opens_popup=True)]),
            main,
            FakeRecorder(events),
            tmp_path / "shots",
            timeout=15.0,
        )


async def test_pause_on_error_stops_on_the_window_that_failed(tmp_path, monkeypatch):
    """`--pause-on-error` zostawia otwarte okno, w którym krok padł.

    Zanim `_Capture.page` stało się „oknem aktywnym", była tylko jedna możliwa
    odpowiedź i pauza dostawała parametr `page` funkcji. Teraz błąd w popupie
    zostawiłby dewelopera przed oknem głównym, na którym nic złego nie widać.
    """

    paused: list[object] = []

    async def _pause(page, *_args, **_kwargs):
        paused.append(page)

    monkeypatch.setattr(capture, "pause_for_inspection", _pause)
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    events: list[str] = []
    main, popup = _windows(events)

    class ExplodingRecorder(FakeRecorder):
        async def point(self, target, ripple=False):
            raise RuntimeError("krok padł w popupie")

    scenario = Scenario(
        config=_cfg(),
        steps=[Step(click="ikona profilu"), Step(click="pole w wyskakującym oknie")],
    )
    with pytest.raises(RuntimeError, match="padł w popupie"):
        await capture_pages(
            scenario,
            _compiled([_click(opens_popup=True), _click()]),
            main,
            FakeRecorder(events),
            tmp_path / "shots",
            timeout=15.0,
            pause_on_error=True,
            make_recorder=lambda page: ExplodingRecorder(events),
        )

    assert paused == [popup]


async def test_close_window_without_a_popup_is_a_no_op(tmp_path):
    """`closeWindow` w scenariuszu bez popupu nie ma czego zamykać.

    Autor pisze go dla filmu; przewodnik nie ma powodu ani padać, ani zamykać
    okna głównego — zostaje sama narracja.
    """

    events: list[str] = []
    main, _popup = _windows(events)
    scenario = Scenario(config=_cfg(), steps=[Step(close_window=True, say="wracamy")])
    pages = await capture_pages(
        scenario,
        _compiled([None]),
        main,
        FakeRecorder(events),
        tmp_path / "shots",
        timeout=15.0,
    )

    assert [p.text for p in pages] == ["wracamy"]
    assert "close:main" not in events
