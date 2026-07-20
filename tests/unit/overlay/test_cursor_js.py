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


CURSOR_JS = files("guidebot_recorder.overlay").joinpath("cursor.js").read_text("utf-8")


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
