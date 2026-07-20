"""Tests for the desktop-opener overlay (``desktop/`` package).

Two layers: the pure ``resolve_icon`` helper (no browser), and the
``DesktopOverlay`` controller against the ``window.__guidebot_desktop`` JS API in
real Chromium, mirroring ``tests/unit/slide/test_slide.py``.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.desktop.desktop import DesktopOverlay, resolve_icon
from guidebot_recorder.models.scenario import DESKTOP_ICON_ALIASES, Desktop

# --- resolve_icon (no browser) ---------------------------------------------


@pytest.mark.parametrize("name", sorted(set(DESKTOP_ICON_ALIASES)))
def test_resolve_icon_returns_packaged_svg_for_every_builtin(name):
    payload = resolve_icon(Desktop(icon=name))

    assert "iconImg" not in payload
    assert payload["iconSvg"].lstrip().startswith("<svg")


def test_resolve_icon_encodes_a_file_as_a_data_url(tmp_path):
    icon = tmp_path / "my-icon.png"
    # A 1x1 PNG is enough — only the encoding path is under test.
    icon.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
    )

    payload = resolve_icon(Desktop(icon="my-icon.png"), base_dir=tmp_path)

    assert "iconSvg" not in payload
    assert payload["iconImg"].startswith("data:image/png;base64,")


def test_resolve_icon_resolves_a_relative_path_against_the_scenario_dir(tmp_path):
    (tmp_path / "assets").mkdir()
    icon = tmp_path / "assets" / "logo.svg"
    icon.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")

    payload = resolve_icon(Desktop(icon="assets/logo.svg"), base_dir=tmp_path)

    assert payload["iconImg"].startswith("data:image/svg+xml;base64,")


def test_resolve_icon_fails_loud_on_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="ani istniejącym plikiem"):
        resolve_icon(Desktop(icon="brak.png"), base_dir=tmp_path)


def test_resolve_icon_rejects_an_unsupported_file_type(tmp_path):
    bad = tmp_path / "icon.bmp"
    bad.write_bytes(b"BM")

    with pytest.raises(ValueError, match="nieobsługiwany format"):
        resolve_icon(Desktop(icon="icon.bmp"), base_dir=tmp_path)


# --- DesktopOverlay against the JS API (Chromium) --------------------------


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        pg = await browser.new_page()
        await pg.set_viewport_size({"width": 1280, "height": 720})
        try:
            yield pg
        finally:
            await browser.close()


def _payload(**over) -> dict[str, str]:
    base = {"color": "#1f3a63", "label": "Przeglądarka", **resolve_icon(Desktop(icon="chrome"))}
    base.update(over)
    return base


async def test_show_mounts_one_desktop_over_an_intact_page(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content('<main id="app">hello</main>')
    await ctl.install(page)

    await ctl.show(page, _payload())

    assert await page.eval_on_selector_all("[data-guidebot-desktop]", "e => e.length") == 1
    assert await page.eval_on_selector("#app", "el => el.textContent") == "hello"


async def test_icon_center_lands_inside_the_top_left_quadrant(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content("<body></body>")
    await ctl.install(page)
    await ctl.show(page, _payload())

    center = await ctl.icon_center(page)

    assert center is not None
    x, y = center
    # The icon sits where a desktop shortcut would — upper-left, not centred.
    assert 0 < x < 640 and 0 < y < 360


async def test_open_window_grows_a_window_node(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content("<body></body>")
    await ctl.install(page)
    await ctl.show(page, _payload())

    assert await ctl.open_window(page, 50) is True
    count = await page.eval_on_selector_all("[data-guidebot-desktop-window]", "e => e.length")
    assert count == 1


async def test_hide_removes_the_desktop(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content("<body></body>")
    await ctl.install(page)
    await ctl.show(page, _payload())

    await ctl.hide(page)

    assert await page.eval_on_selector_all("[data-guidebot-desktop]", "e => e.length") == 0


async def test_label_is_rendered_as_text_not_markup(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content("<body></body>")
    await ctl.install(page)

    await ctl.show(page, _payload(label="<img src=x onerror=alert(1)>"))

    # The label round-trips as literal text; no <img> is injected.
    label_text = await page.eval_on_selector(
        "[data-guidebot-desktop-label]", "el => el.textContent"
    )
    assert label_text == "<img src=x onerror=alert(1)>"
    injected = await page.eval_on_selector_all("[data-guidebot-desktop] img", "els => els.length")
    assert injected == 0


async def test_builtin_svg_is_adopted_as_an_svg_element(page: Page) -> None:
    ctl = DesktopOverlay()
    await page.set_content("<body></body>")
    await ctl.install(page)

    await ctl.show(page, _payload())

    svgs = await page.eval_on_selector_all("[data-guidebot-desktop] svg", "e => e.length")
    assert svgs == 1
