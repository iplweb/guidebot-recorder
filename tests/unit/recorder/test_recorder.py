import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from guidebot_recorder.models.scenario import Scroll
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.recorder import Recorder


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_click_executes_and_moves_cursor(page):
    overlay = Overlay()
    await page.set_content("<button onclick=\"this.textContent='clicked'\">Zaloguj</button>")
    await overlay.install(page)
    rec = Recorder(page, overlay)

    await rec.click(RoleTarget(role="button", name="Zaloguj"))

    assert await page.locator("button").text_content() == "clicked"
    # cursor moved to roughly the center of the button (did not stay at (0,0))
    assert overlay.pos != (0.0, 0.0)


async def test_enter_text_fills(page):
    await page.set_content('<label for="e">E-mail</label><input id="e">')
    rec = Recorder(page, overlay=None)  # compile-mode: no overlay

    await rec.enter_text(RoleTarget(role="textbox", name="E-mail"), "user@x.pl")

    assert await page.locator("#e").input_value() == "user@x.pl"


async def test_click_scrolls_to_horizontally_offscreen_element(page):
    await page.set_viewport_size({"width": 400, "height": 400})
    await page.set_content(
        "<div style='width:3000px;position:relative;height:400px'>"
        "<button style='position:absolute;left:2500px;top:10px' "
        "onclick=\"this.textContent='ok'\">Zaloguj</button></div>"
    )
    rec = Recorder(page, overlay=None)

    await rec.click(RoleTarget(role="button", name="Zaloguj"))

    assert await page.locator("button").text_content() == "ok"


async def test_apply_readiness_none_is_noop(page):
    await page.set_content("<p>ok</p>")
    rec = Recorder(page, overlay=None)
    await rec.apply_readiness("none")  # does not raise


async def test_click_emits_exactly_one_click_sound_even_without_overlay(page):
    await page.set_content("<button>ok</button>")
    events = []
    rec = Recorder(page, None, on_sfx=events.append)  # overlay=None → fallback path
    await rec.click(RoleTarget(role="button", name="ok"))
    assert events == ["click"]


async def test_click_emits_one_click_sound_with_overlay(page):
    await page.set_content("<button>ok</button>")
    events = []
    overlay = Overlay()
    await overlay.install(page)
    rec = Recorder(page, overlay, on_sfx=events.append)
    await rec.click(RoleTarget(role="button", name="ok"))
    assert events == ["click"]


async def test_hover_emits_no_click_sound(page):
    await page.set_content("<button>ok</button>")
    events = []
    rec = Recorder(page, None, on_sfx=events.append)
    await rec.hover(RoleTarget(role="button", name="ok"))
    assert "click" not in events


async def test_enter_text_instant_when_no_delay(page):
    await page.set_content('<label for="i">E</label><input id="i">')
    events = []
    rec = Recorder(page, None, on_sfx=events.append)  # type_delay_ms=None
    await rec.enter_text(RoleTarget(role="textbox", name="E"), "abc")
    assert await page.locator("#i").input_value() == "abc"
    assert events == []  # no key events on instant path


async def test_enter_text_animated_types_char_by_char_and_emits_keys(page):
    await page.set_content('<label for="i">E</label><input id="i">')
    events = []
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=events.append)
    await rec.enter_text(RoleTarget(role="textbox", name="E"), "hi!")
    assert await page.locator("#i").input_value() == "hi!"
    assert events == ["key", "key", "key"]  # exactly len(text)


async def test_enter_text_contenteditable_does_not_crash(page):
    await page.set_content('<div data-testid="d" contenteditable="true"></div>')
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=lambda k: None)
    await rec.enter_text(TestidTarget(testid="d"), "xy")  # input_value() raises → guarded fill
    assert await page.locator('[data-testid="d"]').text_content() == "xy"


async def test_enter_text_control_char_falls_back_to_instant(page):
    await page.set_content('<label for="t">T</label><textarea id="t"></textarea>')
    events = []
    rec = Recorder(page, None, type_delay_ms=1, on_sfx=events.append)
    await rec.enter_text(RoleTarget(role="textbox", name="T"), "a\nb")
    assert await page.locator("#t").input_value() == "a\nb"
    assert events == []  # instant path, no per-char key events


class _FakeLocator:
    def __init__(self, box):
        self._box = box
        self.hovered = False

    async def evaluate(self, _script):
        return None

    async def bounding_box(self):
        return self._box


class _FakeOverlay:
    def __init__(self):
        self.moves = []
        self.ripples = 0

    async def move_to(self, _page, x, y):
        self.moves.append((x, y))

    async def ripple(self, _page, flash=False):
        self.ripples += 1


class _FakePage:
    async def wait_for_timeout(self, _ms):
        return None


@pytest.fixture
def patched_locator(monkeypatch):
    box = {"x": 10, "y": 20, "width": 100, "height": 40}
    loc = _FakeLocator(box)

    async def _fake_build_locator(_frame, _target):
        return loc

    monkeypatch.setattr("guidebot_recorder.recorder.recorder.build_locator", _fake_build_locator)
    return loc, box


async def test_point_returns_center_and_box(patched_locator):
    loc, box = patched_locator
    overlay = _FakeOverlay()
    rec = Recorder(_FakePage(), overlay)
    res = await rec.point(object())  # target unused by the fake build_locator
    assert res.locator is loc
    assert res.box == box
    assert res.center == (60.0, 40.0)  # 10+100/2, 20+40/2


async def test_point_ripple_false_skips_ripple(patched_locator):
    overlay = _FakeOverlay()
    rec = Recorder(_FakePage(), overlay)
    await rec.point(object(), ripple=False)
    assert overlay.ripples == 0
    assert overlay.moves == [(60.0, 40.0)]  # still moves the cursor


async def test_point_no_box_gives_none_center(monkeypatch):
    loc = _FakeLocator(None)

    async def _fake_build_locator(_frame, _target):
        return loc

    monkeypatch.setattr("guidebot_recorder.recorder.recorder.build_locator", _fake_build_locator)
    rec = Recorder(_FakePage(), _FakeOverlay())
    res = await rec.point(object())
    assert res.box is None
    assert res.center is None


_SELECT_HTML = (
    '<select aria-label="Report">'
    "<option>lista</option><option>tabela</option><option>BibTeX</option>"
    "</select>"
)


async def test_select_sets_value_in_compile_mode(page):
    await page.set_content(_SELECT_HTML)
    rec = Recorder(page, overlay=None)  # compile mode: set value directly
    await rec.select(RoleTarget(role="combobox", name="Report"), "tabela")
    assert await page.locator("select").input_value() == "tabela"


async def test_select_native_sets_value_at_once_with_overlay(page):
    # `mode: native` — the two-beat DOM choreography lives in test_recorder_select.py
    overlay = Overlay()
    await page.set_content(_SELECT_HTML)
    await overlay.install(page)
    events = []
    rec = Recorder(page, overlay, on_sfx=events.append)
    await rec.select(RoleTarget(role="combobox", name="Report"), "BibTeX", native=True)
    assert await page.locator("select").input_value() == "BibTeX"
    assert overlay.pos != (0.0, 0.0)  # cursor glided to the control
    assert events == ["click"]  # ripple only — the value is set at once, no stepping


async def test_select_unknown_option_raises(page):
    await page.set_content(_SELECT_HTML)
    rec = Recorder(page, overlay=None)
    with pytest.raises(PlaywrightError):
        await rec.select(RoleTarget(role="combobox", name="Report"), "nie ma takiej")


_TALL_PAGE = "<div style='height:3000px'>tall</div>"


async def test_scroll_down_moves_viewport_with_overlay(page):
    overlay = Overlay()
    await page.set_content(_TALL_PAGE)
    await overlay.install(page)
    rec = Recorder(page, overlay)
    await rec.scroll(Scroll(to="down"))
    assert await page.evaluate("() => window.scrollY") > 0


async def test_scroll_bottom_then_top_compile_mode(page):
    await page.set_content(_TALL_PAGE)
    rec = Recorder(page, overlay=None)  # compile mode: jump directly
    await rec.scroll(Scroll(to="bottom"))
    assert await page.evaluate("() => window.scrollY") > 100
    await rec.scroll(Scroll(to="top"))
    assert await page.evaluate("() => window.scrollY") == 0


async def test_scroll_amount_is_honoured(page):
    await page.set_content(_TALL_PAGE)
    rec = Recorder(page, overlay=None)
    await rec.scroll(Scroll(to="down", amount=200))
    assert await page.evaluate("() => window.scrollY") == 200
