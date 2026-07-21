"""Animacja zakreślania: `Overlay.encircle` i `encircle()` w cursor.js."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.models.config import CursorConfig, Viewport
from guidebot_recorder.overlay.overlay import ENCIRCLE_MAX_MS, ENCIRCLE_MIN_MS, Overlay

# własny atrybut, nie `data-guidebot-highlight` — ten należy do starej,
# niezwiązanej funkcji `highlight()` rysującej prostokąt
TRAIL = "[data-guidebot-encircle]"


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            yield page
        finally:
            await browser.close()


async def _cursor_position(page: Page) -> tuple[float, float]:
    pos = await page.evaluate(
        """() => {
            const el = document.querySelector("[data-guidebot-cursor]");
            return [Number.parseFloat(el.style.left), Number.parseFloat(el.style.top)];
        }"""
    )
    return (pos[0], pos[1])


async def _encircle(page: Page, overlay: Overlay, **kwargs) -> None:
    """Zakreślenie wokół środka kadru — testy różnią się tylko pokrętłami."""

    await overlay.encircle(page, cx=400.0, cy=300.0, rx=120.0, ry=60.0, **kwargs)


async def test_encircle_is_part_of_the_public_cursor_api(page: Page) -> None:
    overlay = Overlay()

    await overlay.install(page)

    assert await page.evaluate("typeof window.__guidebot_cursor.encircle") == "function"


async def test_a_current_version_api_without_encircle_is_replaced(page: Page) -> None:
    """Regresja na guard „poprzednia wersja już jest" w cursor.js.

    Skrypt przy ponownym wstrzyknięciu robi wczesny `return`, gdy zastanie API
    tej samej wersji z kompletem znanych metod. Gdyby lista nazw w tym guardzie
    nie wymieniała `encircle`, starsza wersja przeżyłaby nawigację SPA i strona
    zostałaby bez animacji — awaria nie do odtworzenia lokalnie.
    """

    overlay = Overlay()
    await overlay.install(page)
    version = await page.evaluate("window.__guidebot_cursor.__guidebotVersion")

    await page.evaluate(
        """(version) => {
            window.__guidebot_cursor = {
                __guidebotVersion: version,
                ensure: () => {},
                moveTo: () => {},
                ripple: () => {},
                highlight: () => {},
            };
        }""",
        version,
    )
    await overlay.ensure(page)

    assert await page.evaluate("typeof window.__guidebot_cursor.encircle") == "function"


async def test_encircle_returns_the_cursor_to_the_entry_point(page: Page) -> None:
    overlay = Overlay(CursorConfig(), Viewport(width=800, height=600))
    await overlay.install(page)

    await _encircle(page, overlay, loops=1, hold=0.0, color="#22c55e")

    assert overlay.pos == (520.0, 300.0)
    x, y = await _cursor_position(page)
    assert x == pytest.approx(520.0, abs=1.0)
    assert y == pytest.approx(300.0, abs=1.0)


async def test_encircle_draws_a_trail_in_the_requested_colour_then_cleans_it_up(
    page: Page,
) -> None:
    overlay = Overlay(CursorConfig(), Viewport(width=800, height=600))
    await overlay.install(page)

    # `hold` daje zapas na wolnym CI: bez niego ślad żyje tylko tyle, ile trwa
    # animacja, i między `wait_for` a odczytem atrybutu mógłby zdążyć zniknąć —
    # `get_attribute` wisiałby wtedy do domyślnego timeoutu Playwrighta.
    running = asyncio.create_task(
        _encircle(page, overlay, loops=2, hold=1.0, color="#22c55e"),
    )
    await page.locator(TRAIL).wait_for(state="attached", timeout=5000)
    stroke = await page.locator(TRAIL).get_attribute("stroke")
    await running

    assert stroke == "#22c55e"
    await page.locator(TRAIL).wait_for(state="detached", timeout=5000)


async def test_cursor_keeps_a_steady_speed_around_an_elongated_ellipse(page: Page) -> None:
    """Wokół szerokiej tabeli kursor ma jechać równo, a nie pełznąć i śmigać.

    Parametryzacja kątem daje stałą prędkość *kątową*: przy rx/ry = 8 kursor
    przechodzi w jednej klatce ośmiokrotnie dłuższy odcinek na płaskich łukach
    niż na końcach. Ślad jest rysowany długością łuku, więc rozjeżdża się wtedy
    z kursorem w środku okrążenia. Test mierzy odległości między kolejnymi
    klatkami i pilnuje, żeby rozrzut był mały.
    """

    overlay = Overlay(CursorConfig(), Viewport(width=800, height=600))
    await overlay.install(page)
    await page.evaluate(
        """() => {
            window.__samples = [];
            const tick = () => {
                const el = document.querySelector("[data-guidebot-cursor]");
                if (el) {
                    window.__samples.push([
                        Number.parseFloat(el.style.left),
                        Number.parseFloat(el.style.top),
                    ]);
                }
                window.__sampler = window.requestAnimationFrame(tick);
            };
            tick();
        }"""
    )

    await overlay.encircle(
        page, cx=400.0, cy=300.0, rx=320.0, ry=40.0, loops=1, hold=0.0, color="#22c55e"
    )

    steps = await page.evaluate(
        """() => {
            window.cancelAnimationFrame(window.__sampler);
            const pts = window.__samples;
            const out = [];
            for (let i = 1; i < pts.length; i++) {
                out.push(Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
            }
            return out.filter((d) => d > 0);
        }"""
    )

    steps.sort()
    p10 = steps[len(steps) // 10]
    p90 = steps[len(steps) * 9 // 10]
    assert p90 / p10 < 2.0, f"prędkość kursora skacze {p90 / p10:.1f}× (rx/ry = 8)"


async def test_lap_duration_scales_with_the_ellipse_but_stays_within_its_own_bounds() -> None:
    overlay = Overlay()

    tiny = overlay.lap_duration(rx=8.0, ry=8.0)
    medium = overlay.lap_duration(rx=200.0, ry=120.0)
    huge = overlay.lap_duration(rx=4000.0, ry=2000.0)

    assert tiny == ENCIRCLE_MIN_MS
    assert huge == ENCIRCLE_MAX_MS
    assert ENCIRCLE_MIN_MS < medium < ENCIRCLE_MAX_MS
