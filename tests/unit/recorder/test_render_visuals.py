"""``recorder.render.visuals``: cursor hand-off, close-window return and popup presentation.

Split out of the original ``test_render.py``. ``test_popup_is_composed...`` refers
to ``test_smallest_legal_settle_still_renders`` in ``test_render_narration.py``.
"""

import textwrap
from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder import render as render_module
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.video.mux.probe import probe_duration

from ._render_helpers import FFMPEG, FakeTts, LinkReasoner, LongTts

pytestmark = FFMPEG


class PopupCursorReasoner:
    """Opens the popup, then closes it from inside the popup."""

    async def resolve(self, instruction, candidates):
        if "Zamknij" in instruction:
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="button", name="Zamknij", exact=True),
            )
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


async def test_render_hands_cursor_over_to_popup_and_back(tmp_path, monkeypatch):
    """The cursor lives in exactly one window at a time.

    While a popup is on screen the main window must not keep painting its own
    synthetic cursor (the compositor shows the main video around/behind the
    popup). On popup close the main window takes the cursor back.
    """

    import guidebot_recorder.recorder.render as R

    popup_html = tmp_path / "popup.html"
    popup_html.write_text(
        '<h1>Popup</h1><button onclick="window.close()">Zamknij</button>',
        encoding="utf-8",
    )
    main_html = tmp_path / "main.html"
    main_html.write_text(
        "<button onclick=\"window.open('popup.html')\">Zaloguj</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main_html.resolve().as_uri()}"
          - teach: "kliknij Zaloguj"
          - teach: "kliknij Zamknij w popupie"
          - wait: 0.3
        """
    )
    path = tmp_path / "popup-cursor.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    events: list[tuple[str, str]] = []

    def _role(page) -> str:
        return "popup" if page.url.endswith("popup.html") else "main"

    class SpyOverlay(R.stage.Overlay):
        async def hide(self, page):
            events.append(("hide", _role(page)))
            await super().hide(page)

        async def show(self, page):
            events.append(("show", _role(page)))
            await super().show(page)

    monkeypatch.setattr(R.stage, "Overlay", SpyOverlay)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, PopupCursorReasoner(), selects=None)
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    # The popup's own cursor is never suppressed; only the main window's is.
    assert ("hide", "popup") not in events
    assert ("hide", "main") in events, events
    assert ("show", "main") in events, events
    # Hidden on handover to the popup, revealed again once the popup is gone.
    assert events.index(("hide", "main")) < events.index(("show", "main")), events
    assert events.count(("hide", "main")) == events.count(("show", "main")), events


class _CursorPage:
    """A page that records nothing but its own liveness and viewport."""

    def __init__(self, viewport=None, closed=False):
        self.viewport_size = viewport
        self._closed = closed

    def is_closed(self):
        return self._closed


class _RecordingOverlay:
    """Captures the cursor calls `_hand_cursor_to_popup` makes, in order."""

    def __init__(self, pos=(0.0, 0.0)):
        self.pos = pos
        self.calls = []

    async def hide(self, page):
        self.calls.append(("hide", page))

    async def show(self, page):
        self.calls.append(("show", page))

    async def move_to(self, page, x, y, ms=None):
        self.calls.append(("move_to", page, x, y, ms))
        self.pos = (x, y)


async def test_hand_cursor_to_popup_centres_and_reveals_the_popup_cursor():
    """The popup's cursor must be visible on arrival, not on first action.

    Without this the popup's own cursor instance inherits ``Overlay.pos`` — the
    opener's coordinates, usually the control that opened the popup — and paints
    nothing until something moves it.
    """

    main = _CursorPage()
    popup_page = _CursorPage(viewport={"width": 500, "height": 670})
    popup = render_module._PopupSession(page=popup_page, video=None, opened_at=0.0)
    overlay = _RecordingOverlay(pos=(1300.0, 40.0))

    await render_module._hand_cursor_to_popup(main, popup, overlay)

    assert overlay.calls == [
        ("hide", main),
        ("move_to", popup_page, 250.0, 335.0, 0),
        ("show", popup_page),
    ]
    # ms=0: a glide would animate in from the *other* window's coordinates.
    assert overlay.calls[1][4] == 0


async def test_hand_cursor_to_popup_remembers_where_the_main_cursor_stood():
    """Centring in the popup overwrites the shared position; it must be restorable."""

    popup = render_module._PopupSession(
        page=_CursorPage(viewport={"width": 500, "height": 670}),
        video=None,
        opened_at=0.0,
    )

    await render_module._hand_cursor_to_popup(
        _CursorPage(), popup, _RecordingOverlay(pos=(1300.0, 40.0))
    )

    assert popup.main_cursor_pos == (1300.0, 40.0)


async def test_hand_cursor_to_popup_skips_centring_without_a_viewport():
    """An unknown viewport costs the centring, never the render."""

    popup = render_module._PopupSession(page=_CursorPage(None), video=None, opened_at=0.0)
    overlay = _RecordingOverlay()
    main = _CursorPage()

    await render_module._hand_cursor_to_popup(main, popup, overlay)

    assert overlay.calls == [("hide", main)]


# --- closeWindow ---------------------------------------------------------------


def _write_close_window_scenario(
    tmp_path: Path, *, popup_config: bool = True, chrome: bool = False
) -> Path:
    # A data: page cannot open a new window onto another data: URL (Chromium
    # blocks it outright), so the popup destination needs a real file:// URL —
    # same convention as test_compile.py's closeWindow test. The page paints its
    # own full-bleed background so it genuinely fills the tab: that makes the
    # popup's content bounding box decline outright (see `paintsPage` in
    # `_POPUP_CONTENT_BOX_SCRIPT`) and leaves cropdetect nothing to trim either —
    # matching a real `_blank` tab, where every crop level declines because
    # there is no smaller window to name.
    second = tmp_path / "second.html"
    second.write_text(
        "<!doctype html><html><head><style>body{margin:0;background:#2a6ebb}</style>"
        "</head><body><p>druga</p></body></html>",
        encoding="utf-8",
    )
    main = tmp_path / "main.html"
    main.write_text(
        f"<a href='{second.resolve().as_uri()}' target='_blank'>otworz</a>",
        encoding="utf-8",
    )
    # popup_config=False drops the explicit `popup:` block so the default
    # `float` transition applies — needed to prove a canvas-filling tab gets
    # overridden to `slide` rather than testing a scenario that already asks
    # for `slide`.
    popup_line = "  popup: {transition: slide, slideMs: 40}" if popup_config else ""
    # chrome=True turns the whole browser-chrome feature on, which is what makes
    # the `_blank` tab's address bar observable at all: without it the render
    # builds no `Chrome` controller and there is no bar to mount anywhere.
    chrome_line = "  chrome: {enabled: true}" if chrome else ""
    scenario = textwrap.dedent(
        f"""\
        config:
          title: Karta
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        {popup_line}
        {chrome_line}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy do glownego okna."
        """
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    return path


async def test_close_window_returns_to_main_and_restores_the_cursor(tmp_path):
    path = _write_close_window_scenario(tmp_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()

        out = tmp_path / "out.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    assert probe_duration(out) > 0


async def test_close_window_hands_the_cursor_back_to_its_pre_popup_position(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    restored: list[tuple[float, float] | None] = []
    original = R.visuals._prepare_main_after_popup_close

    async def spy(page, overlay, chrome, settle_ms, restore_cursor_to=None):
        restored.append(restore_cursor_to)
        await original(page, overlay, chrome, settle_ms, restore_cursor_to=restore_cursor_to)

    monkeypatch.setattr(R.visuals, "_prepare_main_after_popup_close", spy)

    path = _write_close_window_scenario(tmp_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert restored, "closeWindow never routed through the popup-close handler"
    assert restored[0] is not None, (
        "the cursor was handed back without its pre-popup position -- the main "
        "window's cursor will be parked at the popup's centre"
    )


async def test_a_full_canvas_popup_is_presented_full_frame_not_inset(tmp_path, monkeypatch):
    # `float` is the default; a `_blank` tab must still render full-frame.
    import guidebot_recorder.recorder.render as R

    seen: list[str | None] = []
    original = R.post.compose_popup_video

    def spy(*args, **kwargs):
        seen.append(kwargs.get("transition"))
        return original(*args, **kwargs)

    monkeypatch.setattr(R.post, "compose_popup_video", spy)

    path = _write_close_window_scenario(tmp_path, popup_config=False)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen == ["slide"], f"expected the full-canvas tab to force slide, got {seen}"


# --- P16 popup composition -> P17 time editing ---------------------------------


async def test_popup_is_composed_before_time_editing_and_feeds_it(tmp_path, monkeypatch):
    """The compositor runs FIRST and the time editor consumes ITS output.

    Popups are composed on the RECORDING axis: `popup.opened_at` / `closed_at`
    are raw wall clock, measured against the same anchor the recording was
    started with. Time editing is what moves the film onto the VIRTUAL axis by
    inserting held frames. Run the two the other way round -- or thread the
    intermediate path wrong, so the editor re-reads the raw recording -- and
    the popup lands at the wrong moment in the finished film.

    Nothing else in the suite notices that, which is the whole reason this test
    exists. The ordering is stated in one comment in `run_render` and asserted
    nowhere:

    * `test_compositor_starts_popup_at_verified_visual_frame` and every
      `compose_popup_video` test in `tests/unit/video/test_mux.py` drive the
      compositor DIRECTLY on synthetic MP4s -- they never go through
      `run_render`, so they cannot see either phase's position in it;
    * `test_a_full_canvas_popup_is_presented_full_frame_not_inset` does spy
      `compose_popup_video` inside a real render, but records only
      `kwargs["transition"]` -- not which file went in or out;
    * `test_hold_frame_film_matches_the_model_exactly` and
      `test_smallest_legal_settle_still_renders` spy `_apply_timeline_edits`,
      but record only the `Timeline`, and neither scenario has a popup at all.

    And every downstream guard compares the film's LENGTH against the model --
    `_apply_timeline_edits`' own frame-count check, the audio beds, the mux
    duration -- i.e. the model against itself. A swap keeps all of those green,
    because the film still comes out exactly as long as the timeline says: it
    just has the popup in the wrong place. Length is not position, so length
    checks cannot catch this. Only the call order and the paths can.

    The scenario needs BOTH phases to fire: `_write_close_window_scenario`
    supplies the popup, and its trailing `say` under `LongTts` (3.0s narration
    against the default 1.0s `holdFrameSettle`) supplies the freeze -- without
    a freeze the timeline is empty and the time-editing branch is skipped
    entirely.
    """
    import guidebot_recorder.recorder.render as R

    order: list[str] = []
    composed: list[tuple[Path, Path]] = []
    edited: list[tuple[Path, Path]] = []

    original_compose = R.post.compose_popup_video
    original_edit = R.timeline._apply_timeline_edits

    def spy_compose(main, popup, dest, *args, **kwargs):
        order.append("compose")
        composed.append((Path(main), Path(dest)))
        return original_compose(main, popup, dest, *args, **kwargs)

    def spy_edit(source, timeline, dest):
        order.append("edit")
        edited.append((Path(source), Path(dest)))
        return original_edit(source, timeline, dest)

    monkeypatch.setattr(R.post, "compose_popup_video", spy_compose)
    monkeypatch.setattr(R.timeline, "_apply_timeline_edits", spy_edit)

    path = _write_close_window_scenario(tmp_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()
        await run_render(path, tmp_path / "out.mp4", LongTts(), tmp_path / "cache", browser)
        await browser.close()

    assert composed, "the popup was never composed -- the scenario lost its popup"
    assert edited, (
        "no time edit ran -- the trailing 3.0s narration produced no freeze, "
        "so this test would pass vacuously"
    )
    assert order == ["compose", "edit"], (
        "popup composition must run before time editing: the popup's opened_at/"
        f"closed_at are raw recording-axis wall clock (got {order})"
    )

    main_webm, composite = composed[0]
    edit_source, _edit_dest = edited[0]
    assert edit_source == composite, (
        "time editing must consume the compositor's output, not the raw "
        f"recording -- it read {edit_source} while the composite is {composite}"
    )
    assert edit_source != main_webm, (
        "time editing read the raw recording -- the composited popup would be "
        "dropped from the film entirely"
    )


# --- the address bar on a `target="_blank"` tab --------------------------------
# `bare_popups` (float/slide) is a context-wide init-script flag, so it strips the
# legacy in-DOM bar from every top-level non-shell document. A genuine `_blank`
# tab is a real browser tab, though, and reads as a rendering fault without an
# address bar — so it, and only it, mounts the bar per page.


async def test_blank_tab_gets_an_address_bar_while_other_popups_stay_bare(tmp_path, monkeypatch):
    # `_blank` is the case where `window.open` was never called at all -- that,
    # not the crop verdict, is what is knowable while the window is still being
    # recorded (the crop chain only answers once the recording is over, and the
    # bar is painted DOM that would corrupt crop levels 2 and 3).
    from guidebot_recorder.chrome import Chrome as ChromeController

    mounted: list[bool] = []
    original = ChromeController.install_bar

    async def spy(self, page):
        await original(self, page)
        mounted.append(await page.query_selector("[data-guidebot-chrome]") is not None)

    monkeypatch.setattr(ChromeController, "install_bar", spy)

    path = _write_close_window_scenario(tmp_path, chrome=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert mounted == [True], (
        "a `_blank` tab must mount the legacy address bar exactly once, and it "
        f"must actually be in the popup's DOM afterwards (got {mounted!r})"
    )


async def test_sized_window_open_popup_never_mounts_the_address_bar(tmp_path, monkeypatch):
    # The counterpart: a popup the site really did open with `window.open` stays
    # bare, exactly as today -- the compositor frames it in post-process. This is
    # what pins the per-window seam as per-window rather than a global flip.
    from guidebot_recorder.chrome import Chrome as ChromeController

    calls: list[str] = []

    async def spy(self, page):
        calls.append(page.url)

    monkeypatch.setattr(ChromeController, "install_bar", spy)

    second = tmp_path / "second.html"
    second.write_text(
        "<!doctype html><html><head><style>body{margin:0;background:#2a6ebb}</style>"
        "</head><body><p>druga</p></body></html>",
        encoding="utf-8",
    )
    main = tmp_path / "main.html"
    main.write_text(
        "<a href='#' onclick=\"window.open("
        f"'{second.resolve().as_uri()}','p','width=420,height=300');return false\">otworz</a>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: Popup
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
          popup: {{transition: slide, slideMs: 40}}
          chrome: {{enabled: true}}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy do glownego okna."
        """
    )
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, LinkReasoner(), selects=None)
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert calls == [], f"a sized window.open popup must stay bare (install_bar called for {calls})"
