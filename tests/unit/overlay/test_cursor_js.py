"""Direct tests of cursor.js's public API (no Python Overlay wrapper).

Injects ``window.__guidebot_cursor_config`` then evaluates the raw script,
exercising the ``CFG.start`` seed, the configurable/optional-flash ripple, and
the persistent ``hidden`` flag added in Task 1.1.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import Page, async_playwright


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page()
        try:
            yield pg
        finally:
            await b.close()


# One IIFE stored as two contiguous fragments (``cursor.js`` +
# ``cursor_effects.js``); concatenate in this order to reconstruct the script,
# exactly as ``Overlay`` does.
_CURSOR_PKG = files("guidebot_recorder.overlay")
CURSOR_JS = _CURSOR_PKG.joinpath("cursor.js").read_text("utf-8") + _CURSOR_PKG.joinpath(
    "cursor_effects.js"
).read_text("utf-8")


async def _inject(page: Page, cfg: dict) -> None:
    await page.evaluate(f"window.__guidebot_cursor_config = {json.dumps(cfg)};")
    await page.evaluate(CURSOR_JS)


async def test_start_seed_centers_first_mount(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"start": [400, 300]})
    pos = await page.evaluate("window.__guidebot_cursor.position")
    assert pos == [400, 300]


async def test_ripple_flash_draws_filled_disc_only_when_configured_and_requested(
    page: Page,
) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"click": {"color": "rgb(1,2,3)", "scale": 5, "flash": True}})
    # ripple(true) synchronously creates the ring (+ flash disc); read immediately.
    n = await page.evaluate(
        "() => { window.__guidebot_cursor.ripple(true);"
        " return document.querySelectorAll('[data-guidebot-ripple],[data-guidebot-flash]').length; }"
    )
    assert n >= 2  # ring + flash
    # flash=false → ring only
    n2 = await page.evaluate(
        "() => { document.querySelectorAll('[data-guidebot-flash]').forEach(e=>e.remove());"
        " window.__guidebot_cursor.ripple(false);"
        " return document.querySelectorAll('[data-guidebot-flash]').length; }"
    )
    assert n2 == 0


# Chrome serializes computed `contain` using the shorthand keywords, so
# `layout style paint` comes back as `content`. Expand before asserting.
_CONTAIN_SHORTHANDS = {
    "content": {"layout", "paint", "style"},
    "strict": {"layout", "paint", "size", "style"},
}


def _contain_keywords(computed: str) -> set[str]:
    out: set[str] = set()
    for token in computed.split():
        out |= _CONTAIN_SHORTHANDS.get(token, {token})
    return out


async def test_cursor_host_does_not_paint_contain(page: Page) -> None:
    """`contain: paint` clips the drop-shadow glow to the 34x46 host box."""
    await page.set_content("<div></div>")
    await _inject(page, {})
    contain = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).contain"
    )
    keywords = _contain_keywords(contain)
    assert "paint" not in keywords, f"glow is clipped by contain: {contain!r}"
    # layout/style isolation is what the declaration is there for; keep it.
    assert {"layout", "style"} <= keywords, f"lost isolation: {contain!r}"


# --- Arc motion (Part B) ---------------------------------------------------
#
# Mid-flight geometry must be read from the *rendered* box (computed
# left/top), never from `state.x`/`state.y`: those hold the target from the
# instant `moveTo` is entered.
_SAMPLE_MOVE = """
async ([x, y, ms]) => {
  const el = document.querySelector('[data-guidebot-cursor]');
  const read = () => {
    const cs = getComputedStyle(el);
    return [Number.parseFloat(cs.left), Number.parseFloat(cs.top)];
  };
  const samples = [];
  let running = true;
  const tick = () => {
    if (!running) { return; }
    samples.push(read());
    window.requestAnimationFrame(tick);
  };
  window.requestAnimationFrame(tick);
  await window.__guidebot_cursor.moveTo(x, y, ms);
  running = false;
  return { samples: samples, final: read() };
}
"""

_READ_RENDERED = """() => {
  const cs = getComputedStyle(document.querySelector('[data-guidebot-cursor]'));
  return [Number.parseFloat(cs.left), Number.parseFloat(cs.top)];
}"""


def _peak_off_axis(samples: list[list[float]]) -> float:
    """Largest signed y-deviation seen during a purely horizontal move."""
    assert samples, "no frames were sampled mid-flight"
    return max(samples, key=lambda s: abs(s[1]))[1]


async def test_path_bows_off_the_straight_line(page: Page) -> None:
    """A long move must not trace the exact segment A->B."""
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0.12})
    result = await page.evaluate(_SAMPLE_MOVE, [600, 0, 700])
    peak = abs(_peak_off_axis(result["samples"]))
    # control point sits min(0.12*600, 90) px off-axis -> apex near half of that
    assert 5 < peak < 90, f"expected a bowed path, peak |y| was {peak}"


async def test_bow_direction_is_deterministic(page: Page) -> None:
    """The seeded PRNG must pick the same side for the same move, every time."""
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0.12})
    signs = []
    for _ in range(2):
        await page.evaluate("() => window.__guidebot_cursor.moveTo(0, 0, 0)")
        result = await page.evaluate(_SAMPLE_MOVE, [600, 0, 700])
        peak = _peak_off_axis(result["samples"])
        assert abs(peak) > 5, f"path did not bow at all: {peak}"
        signs.append(peak > 0)
    assert signs[0] == signs[1], "bow flipped sides between identical moves"


async def test_move_lands_exactly_on_target(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0.12})
    result = await page.evaluate(_SAMPLE_MOVE, [617, 349, 400])
    assert result["final"] == [617, 349]


async def test_short_moves_stay_straight(page: Page) -> None:
    """Below ARC_MIN_DISTANCE an arc reads as a twitch; bow must be zero."""
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0.12})
    result = await page.evaluate(_SAMPLE_MOVE, [20, 0, 400])
    peak = abs(_peak_off_axis(result["samples"]))
    assert peak < 0.5, f"short hop should be straight, peak |y| was {peak}"


async def test_bow_zero_disables_arcing(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0})
    result = await page.evaluate(_SAMPLE_MOVE, [600, 0, 700])
    peak = abs(_peak_off_axis(result["samples"]))
    assert peak < 0.5, f"bow: 0 should be a straight line, peak |y| was {peak}"


async def test_zero_duration_still_snaps(page: Page) -> None:
    """`duration === 0` is the post-document-swap position restore.

    The read happens in the *same* synchronous turn as the call: awaiting the
    returned promise first would also pass against an animated path, which is
    exactly what this invariant forbids.
    """
    await page.set_content("<div></div>")
    await _inject(page, {"bow": 0.12})
    pos = await page.evaluate(
        "() => { window.__guidebot_cursor.moveTo(455, 321, 0);"
        " const cs = getComputedStyle(document.querySelector('[data-guidebot-cursor]'));"
        " return [Number.parseFloat(cs.left), Number.parseFloat(cs.top)]; }"
    )
    assert pos == [455, 321]


async def test_unparsable_easing_falls_back_instead_of_throwing(page: Page) -> None:
    await page.set_content("<div></div>")
    warnings: list[str] = []
    page.on("console", lambda msg: warnings.append(msg.text) if msg.type == "warning" else None)
    await _inject(page, {"bow": 0.12, "easing": "wobbly(1,2,3)"})
    result = await page.evaluate(_SAMPLE_MOVE, [600, 0, 400])
    assert result["final"] == [600, 0]
    assert any("wobbly(1,2,3)" in text for text in warnings), warnings


async def test_hidden_flag_survives_ensure(page: Page) -> None:
    await page.set_content("<div></div>")
    await _inject(page, {})
    await page.evaluate("window.__guidebot_cursor.hide()")
    await page.evaluate("window.__guidebot_cursor.ensure()")
    disp = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp == "none"
    await page.evaluate("window.__guidebot_cursor.show()")
    disp2 = await page.evaluate(
        "getComputedStyle(document.querySelector('[data-guidebot-cursor]')).display"
    )
    assert disp2 == "block"


# --- the target outline, and the one property that is the whole point ---------


async def test_frame_is_stacked_below_the_cursor(page: Page) -> None:
    """Ramka musi mieć NIŻSZY z-index niż kursor. To jest cała ta funkcja.

    Ramka przeniosła się z SVG (rysowanego nad gotowym PNG) do strony właśnie po
    to, żeby kursor został na wierzchu — zrzut nie ma warstw, więc później już
    nic się pod niego nie wsunie. Gdyby ktoś podniósł ten z-index do maksimum,
    obie warstwy zrównałyby się i o wyniku decydowałaby kolejność dopisania do
    DOM — czyli błąd wracałby losowo. Porównanie liczbowe, nie dosłowna wartość:
    test ma pilnować relacji, a nie utrwalać stałej.
    """

    await page.set_content("<div></div>")
    await _inject(page, {})
    z = await page.evaluate(
        "() => { window.__guidebot_cursor.frame(10, 20, 100, 40);"
        " const zi = (sel) => Number(getComputedStyle(document.querySelector(sel)).zIndex);"
        " return [zi('[data-guidebot-frame]'), zi('[data-guidebot-cursor]')]; }"
    )
    assert z[0] < z[1], f"ramka {z[0]} musi być pod kursorem {z[1]}"


async def test_frame_straddles_the_targets_edge_like_the_svg_stroke_it_replaced(
    page: Page,
) -> None:
    """Obrys ma leżeć okrakiem na krawędzi celu, tak jak robił to `stroke` w SVG.

    CSS rysuje `border` do wewnątrz, a `stroke` w SVG centruje na ścieżce — więc
    bez rozsunięcia pudełka o połowę grubości ramka byłaby ciaśniejsza niż przed
    przenosinami. Sprawdzamy prostokąt zewnętrzny: 4 px obrysu ma wystawać po
    2 px na stronę.
    """

    await page.set_content("<div></div>")
    await _inject(page, {})
    rect = await page.evaluate(
        "() => { window.__guidebot_cursor.frame(10, 20, 100, 40);"
        " const r = document.querySelector('[data-guidebot-frame]').getBoundingClientRect();"
        " return [r.x, r.y, r.width, r.height]; }"
    )
    assert rect == [8, 18, 104, 44]


async def test_a_second_frame_replaces_the_first(page: Page) -> None:
    """Jeden krok obrysowuje jeden cel — resztka po poprzednim kłamałaby na zdjęciu."""

    await page.set_content("<div></div>")
    await _inject(page, {})
    count = await page.evaluate(
        "() => { window.__guidebot_cursor.frame(0, 0, 10, 10);"
        " window.__guidebot_cursor.frame(50, 50, 20, 20);"
        " return document.querySelectorAll('[data-guidebot-frame]').length; }"
    )
    assert count == 1


async def test_clear_frame_takes_the_outline_down_and_tolerates_no_frame(page: Page) -> None:
    """Sprzątanie musi być idempotentne: `_framed_screenshot` woła je z `finally`."""

    await page.set_content("<div></div>")
    await _inject(page, {})
    left = await page.evaluate(
        "() => { window.__guidebot_cursor.frame(10, 20, 100, 40);"
        " window.__guidebot_cursor.clearFrame();"
        " window.__guidebot_cursor.clearFrame();"
        " return document.querySelectorAll('[data-guidebot-frame]').length; }"
    )
    assert left == 0


async def test_frame_rejects_bounds_that_would_silently_draw_nothing(page: Page) -> None:
    """Ujemny rozmiar albo NaN to błąd wywołania, nie niewidzialny prostokąt."""

    await page.set_content("<div></div>")
    await _inject(page, {})
    for args in ("10, 20, -1, 40", "NaN, 0, 10, 10"):
        with pytest.raises(Exception, match="frame bounds"):
            await page.evaluate(f"() => window.__guidebot_cursor.frame({args})")
