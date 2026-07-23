"""E2E: active-page compile and main → popup → main video assembly."""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.video.mux.probe import probe_duration

from ._popup_e2e import (
    FIXTURE,
    FLOATING_SCENARIO_TEMPLATE,
    PYTESTMARK,
    FakeTts,
    NoCallsReasoner,
    PopupReasoner,
    _audio_streams,
    _decode_audio_stream,
    _has_audio_signal,
    _is_chrome_gray,
    _is_main_blue,
    _is_popup_yellow,
    _rgb_at,
    _rgb_at_pixel,
    _stream_types,
)

pytestmark = PYTESTMARK

SCENARIO_TEMPLATE = """\
config:
  title: Popup logowania
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v-pl, lang: pl-PL, trackLanguage: pol, title: Polski}}
  audioTracks:
    - {{provider: fake, voice: v-en, lang: en-US, trackLanguage: eng, title: English}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
    translations: {{en-US: "Open the login popup"}}
  - teach: "przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl"
    translations: {{en-US: "switch to the popup and type koparka@poczta.wp.pl in email"}}
  - wait: 0.4
  - click: "Zamknij popup logowania"
  - click: "Zakończ na stronie głównej"
"""

OPEN_POPUP_SCENARIO_TEMPLATE = """\
config:
  title: Popup pozostający otwarty
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
  - teach: "Wpisz w pole email tekst koparka@poczta.wp.pl"
  - wait: 0.4
"""


SLIDE_SCENARIO_TEMPLATE = """\
config:
  title: Popup logowania (slide)
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{transition: slide, slideMs: 200}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - teach: "Otwórz popup logowania"
  - teach: "przełącz się na popup i wpisz w pole email tekst koparka@poczta.wp.pl"
  - wait: 0.4
  - click: "Zamknij popup logowania"
  - click: "Zakończ na stronie głównej"
"""


async def test_popup_compile_reuse_and_render_composite(tmp_path: Path) -> None:
    path = tmp_path / "popup.scenario.yaml"
    path.write_text(SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()), encoding="utf-8")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        reasoner = PopupReasoner()
        await run_compile(path, compile_page, reasoner, selects=None)
        await compile_page.context.close()

        compiled = load_compiled(compiled_path(path))
        opening = compiled.actions[2]
        typing = compiled.actions[3]
        assert opening is not None and opening.opens_popup is True
        assert typing is not None and typing.action == "type"
        assert typing.input_text == "koparka@poczta.wp.pl"
        assert reasoner.popup_candidates_seen is True
        assert reasoner.calls == 4

        # Reuse still executes the opening click, follows the popup and validates
        # popup-only identities without asking the Reasoner again.
        reuse_page = await browser.new_page()
        await run_compile(path, reuse_page, NoCallsReasoner(), selects=None)
        await reuse_page.context.close()

        out = tmp_path / "popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 2
    audio_streams = _audio_streams(out)
    assert [stream["tags"]["language"] for stream in audio_streams] == ["pol", "eng"]
    assert [stream["tags"]["handler_name"] for stream in audio_streams] == [
        "Polski",
        "English",
    ]
    assert [stream["disposition"]["default"] for stream in audio_streams] == [1, 0]
    assert _decode_audio_stream(out, 0) != _decode_audio_stream(out, 1)

    samples = [_rgb_at(out, duration * fraction / 20) for fraction in range(1, 20)]
    main_indices = [index for index, rgb in enumerate(samples) if _is_main_blue(rgb)]
    popup_indices = [index for index, rgb in enumerate(samples) if _is_popup_yellow(rgb)]
    assert main_indices and popup_indices
    assert min(main_indices) < min(popup_indices)
    assert max(main_indices) > max(popup_indices)
    popup_sample_time = duration * (min(popup_indices) + 1) / 20
    before_popup_windows = [index / 10 for index in range(1, int(popup_sample_time * 10))]
    popup_windows = [index / 10 for index in range(int(popup_sample_time * 10), int(duration * 10))]
    assert any(_has_audio_signal(out, start) for start in before_popup_windows)
    assert any(_has_audio_signal(out, start) for start in popup_windows)
    for sample_index in (min(main_indices), min(popup_indices), max(main_indices)):
        sample_time = duration * (sample_index + 1) / 20
        assert _is_chrome_gray(_rgb_at_pixel(out, sample_time))


async def test_floating_popup_renders_as_inset_over_visible_main(tmp_path: Path) -> None:
    """Floating mode: the bare popup renders end-to-end (prime stabilizes without
    a chrome bar) and composites as a centred inset over the still-visible main
    page — never the full-frame hard cut."""

    path = tmp_path / "floating-popup.scenario.yaml"
    path.write_text(
        FLOATING_SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "floating-popup.mp4"
        # A raise here (e.g. "nie udało się zainicjować warstw wizualnych") would
        # mean the bare-popup prime seam regressed; reaching the asserts proves it
        # stabilized without a [data-guidebot-chrome] bar on the popup page.
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 1

    # Sample centre vs left edge across the timeline. The defining property of the
    # floating composite is that the dimmed MAIN page (blue) shows at the border
    # while the popup (not blue) fills the centre inset. A hard cut would paint the
    # popup edge-to-edge, so the border is never the main page during the popup
    # interval. We key on the main-blue backdrop rather than the popup's exact
    # centre colour, which depends on how the popup content renders (fonts/layout
    # differ across platforms) and is not a reliable single-pixel signal.
    inset_frame_found = False
    for fraction in range(1, 40):
        seconds = duration * fraction / 40
        centre = _rgb_at_pixel(out, seconds, x=319, y=239)
        edge = _rgb_at_pixel(out, seconds, x=4, y=239)
        if _is_main_blue(edge) and not _is_main_blue(centre):
            inset_frame_found = True
            break
    assert inset_frame_found, "expected a floating popup inset over a visible main backdrop"


async def test_slide_popup_renders_full_frame_over_switched_window(tmp_path: Path) -> None:
    """Slide mode: the bare popup renders end-to-end (prime stabilizes without a
    chrome bar) and, unlike float, takes over the FULL frame while active — the
    window slid in as a push, not an inset."""

    path = tmp_path / "slide-popup.scenario.yaml"
    path.write_text(
        SLIDE_SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "slide-popup.mp4"
        # A raise here (prime seam regressed) would abort; reaching the asserts
        # proves the bare-popup prime stabilized without a chrome bar.
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert out.exists()
    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 1

    # Slide takes over full-frame during the interval: find a frame where NEITHER
    # centre NOR border is the main page (blue). That distinguishes slide from
    # float (whose border stays dimmed main-blue) and, combined with the render
    # completing, proves the slide composite ran with the bare-popup seam intact.
    full_frame_found = False
    for fraction in range(1, 40):
        seconds = duration * fraction / 40
        centre = _rgb_at_pixel(out, seconds, x=319, y=239)
        edge = _rgb_at_pixel(out, seconds, x=4, y=239)
        if not _is_main_blue(centre) and not _is_main_blue(edge):
            full_frame_found = True
            break
    assert full_frame_found, "expected a full-frame popup interval (slide takeover)"


async def test_popup_left_open_stays_visible_through_video_end(tmp_path: Path) -> None:
    path = tmp_path / "open-popup.scenario.yaml"
    path.write_text(
        OPEN_POPUP_SCENARIO_TEMPLATE.format(url=FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "open-popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    duration = probe_duration(out)
    assert duration > 0
    stream_types = _stream_types(out)
    assert stream_types.count("video") == 1
    assert stream_types.count("audio") == 1

    samples = [_rgb_at(out, duration * fraction / 20) for fraction in range(1, 20)]
    main_indices = [index for index, rgb in enumerate(samples) if _is_main_blue(rgb)]
    popup_indices = [index for index, rgb in enumerate(samples) if _is_popup_yellow(rgb)]
    assert main_indices and popup_indices
    assert min(main_indices) < min(popup_indices)
    assert _is_popup_yellow(samples[-1])


SMALL_WINDOW_FIXTURE = Path(__file__).parent / "fixtures" / "popup-main-small-window.html"


async def test_floating_popup_frame_is_cropped_to_the_requested_window(tmp_path: Path) -> None:
    """The framed popup is the size the site asked ``window.open`` for.

    ``record_video_size`` is context-level, so the popup records onto a
    640x480 canvas even though the site requested a 320x240 window. Without the
    post-production crop the float mode frames that whole canvas and the window
    comes out as wide as the main page. Cropped, the 320x240 window scales to
    ~230x172 and spans x≈205..435; the uncropped one would span x≈90..550.
    """

    path = tmp_path / "small-window-popup.scenario.yaml"
    path.write_text(
        FLOATING_SCENARIO_TEMPLATE.format(url=SMALL_WINDOW_FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "small-window-popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    duration = probe_duration(out)
    assert duration > 0

    # (230, 185) is inside the cropped window (popup yellow); (140, 110) is
    # outside it but *inside* the frame an uncropped popup would occupy, so it
    # must stay dimmed main-blue for the whole popup interval.
    popup_on_screen = False
    for fraction in range(1, 40):
        seconds = duration * fraction / 40
        if not _is_popup_yellow(_rgb_at_pixel(out, seconds, x=230, y=185)):
            continue  # popup not fully on screen yet (or already closed)
        popup_on_screen = True
        outside = _rgb_at_pixel(out, seconds, x=140, y=110)
        assert _is_main_blue(outside), (
            f"the popup frame reaches (140, 110) at {seconds:.2f}s ({outside}) — "
            "it was not cropped to the requested 320x240 window"
        )
    assert popup_on_screen, "expected the floating popup to be on screen at some point"
