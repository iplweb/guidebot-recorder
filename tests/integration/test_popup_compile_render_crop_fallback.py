"""E2E: the popup-crop fallback chain, on real recordings.

A featureless ``window.open(url, name)`` states no geometry at all, so level 1
has nothing to say and the popup fills the whole recording canvas. What the
frame should then be depends on the popup itself, and the two cases below are
the two honest answers.
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.video.mux.probe import probe_duration

from ._popup_e2e import (
    FLOATING_SCENARIO_TEMPLATE,
    PYTESTMARK,
    FakeTts,
    PopupReasoner,
    _is_main_blue,
    _is_popup_yellow,
    _rgb_at_pixel,
)

pytestmark = PYTESTMARK

FEATURELESS_CARD_FIXTURE = Path(__file__).parent / "fixtures" / "popup-main-featureless-card.html"
FEATURELESS_FULL_BLEED_FIXTURE = Path(__file__).parent / "fixtures" / "popup-main-featureless.html"


async def test_featureless_popup_is_cropped_to_its_content(tmp_path: Path) -> None:
    """No ``window.open`` features: the popup's own content decides the frame.

    The popup is a 300px card on an unpainted page, so levels 2 and 3 both see a
    ~300x290 window at the origin (verified: the DOM box and cropdetect agree
    within a pixel). Cropped, it scales to ~216x208 and spans x≈212..428;
    uncropped it would span x≈90..550, exactly the bug this chain closes.
    """

    path = tmp_path / "featureless-popup.scenario.yaml"
    path.write_text(
        FLOATING_SCENARIO_TEMPLATE.format(url=FEATURELESS_CARD_FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "featureless-popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    duration = probe_duration(out)
    assert duration > 0

    popup_on_screen = False
    for fraction in range(1, 40):
        seconds = duration * fraction / 40
        if not _is_popup_yellow(_rgb_at_pixel(out, seconds, x=300, y=240)):
            continue  # popup not fully on screen yet (or already closed)
        popup_on_screen = True
        outside = _rgb_at_pixel(out, seconds, x=140, y=110)
        assert _is_main_blue(outside), (
            f"the popup frame reaches (140, 110) at {seconds:.2f}s ({outside}) — "
            "a featureless popup was not cropped to its content"
        )
    assert popup_on_screen, "expected the floating popup to be on screen at some point"


async def test_full_bleed_featureless_popup_renders_uncropped(tmp_path: Path) -> None:
    """The documented limitation, pinned: full-bleed content cannot be cropped.

    The popup paints its own page background, so its window genuinely *is* the
    whole canvas: level 2 declines (a painted ``body``), and level 3 declines too
    (the ink it can see does not start at the canvas origin, so it is not a
    window inside padding). Cropping to the ink would cut the popup's background
    away — this test exists to prove we render the full canvas instead of
    inventing a frame.
    """

    path = tmp_path / "full-bleed-popup.scenario.yaml"
    path.write_text(
        FLOATING_SCENARIO_TEMPLATE.format(url=FEATURELESS_FULL_BLEED_FIXTURE.resolve().as_uri()),
        encoding="utf-8",
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        compile_page = await browser.new_page()
        await run_compile(path, compile_page, PopupReasoner(), selects=None)
        await compile_page.context.close()

        out = tmp_path / "full-bleed-popup.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    duration = probe_duration(out)
    assert duration > 0

    # Uncropped, the full-canvas popup scales to ~460x345 and reaches (140, 110):
    # the popup's own yellow, not the dimmed backdrop a cropped frame would leave.
    reached_uncropped_extent = any(
        _is_popup_yellow(_rgb_at_pixel(out, duration * fraction / 40, x=140, y=110))
        for fraction in range(1, 40)
    )
    assert reached_uncropped_extent, (
        "expected the full-bleed popup to be framed at full canvas size"
    )
