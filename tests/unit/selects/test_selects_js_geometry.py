"""Geometry: hostile page CSS, style pinning, re-pinning on scroll, row counting.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map. The overlay is position-fixed and pinned to the select's rect every frame,
so everything here is about the two ways that pin can be lost: a page stylesheet
reaching into the overlay, and the page moving underneath it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import NESTED, _inject, _options, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


# Everything `cursor.js:257-277` defends against, plus what breaks the list's
# row-height maths. Inline `!important` outranks author `!important`, so every
# property the overlay declares itself already wins; these are the ones it must
# declare in the first place.
HOSTILE_CSS = (
    "<style>div,span{opacity:.25!important;visibility:hidden!important;"
    "transform:scale(3)!important;filter:blur(4px)!important;clip-path:inset(50%)!important;"
    "contain:paint!important;font-size:44px!important;border:7px solid red!important;"
    "height:120px!important;min-height:120px!important;position:absolute!important;"
    "float:left!important}</style>"
)

_READ_OVERLAY_STYLES = """() => {
  const api = window.__guidebot_selects;
  const s = document.getElementById('s');
  const button = api.buttonFor(s);
  const list = api.listFor(s);
  const row = list.querySelector('[data-guidebot-option-index="0"]');
  const read = (el) => {
    const cs = getComputedStyle(el);
    return {
      opacity: cs.opacity,
      visibility: cs.visibility,
      transform: cs.transform,
      filter: cs.filter,
      clipPath: cs.clipPath,
    };
  };
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return [r.left, r.top, r.width, r.height];
  };
  return {
    button: read(button),
    list: read(list),
    row: read(row),
    label: read(button.firstElementChild),
    buttonContain: getComputedStyle(button).contain,
    listContain: getComputedStyle(list).contain,
    rowPosition: getComputedStyle(row).position,
    rowFontSize: getComputedStyle(row).fontSize,
    listFontSize: getComputedStyle(list).fontSize,
    rowHeight: row.getBoundingClientRect().height,
    buttonRect: rect(button),
    selectRect: rect(s),
    maxHeight: Number.parseFloat(getComputedStyle(list).maxHeight),
    listHeight: list.getBoundingClientRect().height,
    visibleOptions: Array.from(list.querySelectorAll('[data-guidebot-option-index]'))
      .filter((r) => {
        const lr = list.getBoundingClientRect();
        const rr = r.getBoundingClientRect();
        return rr.top >= lr.top - 1 && rr.bottom <= lr.bottom + 1;
      }).length,
  };
}"""


async def test_page_css_cannot_bleed_into_the_overlay(page: Page) -> None:
    """I4: for a feature whose whole point is on-camera visibility, this is silent.

    Measured before the fix: a page rule `div {opacity:.25!important}` renders the
    shim button at 25 % opacity.
    """
    labels = [f"Opcja {i}" for i in range(30)]
    await page.set_content(
        f"<body style='margin:0'>{HOSTILE_CSS}"
        f"<select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    styles = await page.evaluate(_READ_OVERLAY_STYLES)

    for part in ("button", "list", "row", "label"):
        assert styles[part]["opacity"] == "1", (part, styles[part])
        assert styles[part]["visibility"] == "visible", (part, styles[part])
        assert styles[part]["transform"] == "none", (part, styles[part])
        assert styles[part]["filter"] == "none", (part, styles[part])
        assert styles[part]["clipPath"] == "none", (part, styles[part])
    assert "paint" not in styles["buttonContain"], styles["buttonContain"]
    assert "paint" not in styles["listContain"], styles["listContain"]

    # The row-height maths behind `layoutList` must survive a hostile `div` rule.
    assert styles["rowPosition"] == "static", styles["rowPosition"]
    assert styles["rowFontSize"] == styles["listFontSize"], styles
    assert styles["rowHeight"] < 40, styles["rowHeight"]
    drift = zip(styles["buttonRect"], styles["selectRect"], strict=True)
    assert max(abs(a - b) for a, b in drift) < 1.0, styles
    assert styles["maxHeight"] < 400, styles["maxHeight"]

    # `max-height` alone proves nothing: the `div {height:120px!important}` in
    # `HOSTILE_CSS` at the top of this file wins
    # over an unpinned `height` and shrinks the *rendered* list to 120 px, which
    # measured as 4 visible options out of the 8 `maxVisibleOptions` asks for.
    assert abs(styles["listHeight"] - styles["maxHeight"]) < 1.0, (
        f"a page height rule shrank the list to {styles['listHeight']}px "
        f"(max-height {styles['maxHeight']}px)"
    )
    assert styles["visibleOptions"] >= 8, (
        f"only {styles['visibleOptions']} options fit, maxVisibleOptions was 8"
    )


async def test_geometry_repins_after_a_scroll(page: Page) -> None:
    await page.set_content(
        "<body style='margin:0'><div style='height:300px'></div>"
        f"<select id='s' style='width:220px'>{_options(['a', 'b', 'c'])}</select>"
        "<div style='height:2000px'></div></body>"
    )
    await _inject(page)
    before = await page.evaluate(
        "() => window.__guidebot_selects.buttonFor(document.getElementById('s'))"
        ".getBoundingClientRect().top"
    )
    await page.evaluate("() => window.scrollTo(0, 180)")
    await page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )
    after = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const b = window.__guidebot_selects.buttonFor(s);
      return {button: b.getBoundingClientRect().top, select: s.getBoundingClientRect().top};
    }"""
    )
    assert abs(after["button"] - before) > 100, "the overlay did not move with the page"
    assert abs(after["button"] - after["select"]) < 1.0, "the overlay lost its pin"


async def test_a_scroll_that_moves_nothing_writes_no_styles(page: Page) -> None:
    """M10: the scroll handler forced a write per shim per event, and the rAF loop repins anyway."""
    await page.set_content(NESTED)
    await _inject(page)
    writes = await page.evaluate(
        """() => {
      const proto = CSSStyleDeclaration.prototype;
      const original = proto.setProperty;
      let count = 0;
      proto.setProperty = function (...args) {
        count += 1;
        return original.apply(this, args);
      };
      try {
        // Synchronous dispatch: no animation frame can run in between.
        for (let i = 0; i < 5; i += 1) {
          document.getElementById('row').dispatchEvent(new Event('scroll'));
        }
      } finally {
        proto.setProperty = original;
      }
      return count;
    }"""
    )
    assert writes == 0, f"{writes} style writes for a scroll that moved nothing"


async def test_scroll_inside_a_container_repins_through_the_capture_phase(page: Page) -> None:
    """`scroll` does not bubble, so only a capture-phase listener sees this one."""
    await page.set_content(
        "<body style='margin:0'><div id='box' style='height:150px;overflow:auto'>"
        "<div style='height:60px'></div>"
        f"<select id='s' style='width:220px'>{_options(['a', 'b', 'c'])}</select>"
        "<div style='height:600px'></div></div></body>"
    )
    await _inject(page)
    moved = await page.evaluate(
        """() => {
      const box = document.getElementById('box');
      const s = document.getElementById('s');
      const button = window.__guidebot_selects.buttonFor(s);
      const before = button.getBoundingClientRect().top;
      box.scrollTop = 50;
      // The browser fires the real `scroll` asynchronously, so nothing has
      // repinned yet at this point.
      const beforeEvent = button.getBoundingClientRect().top;
      box.dispatchEvent(new Event('scroll'));
      return {
        before: before,
        beforeEvent: beforeEvent,
        after: button.getBoundingClientRect().top,
        selectTop: s.getBoundingClientRect().top,
      };
    }"""
    )
    assert abs(moved["beforeEvent"] - moved["before"]) < 1.0, "something else repinned first"
    assert abs(moved["selectTop"] - moved["before"]) > 10, "the select did not actually move"
    assert abs(moved["after"] - moved["selectTop"]) < 1.0, "the capture-phase listener never ran"


async def test_max_visible_options_counts_options_not_optgroup_headings(page: Page) -> None:
    """M10: headings pushed real options below the configured visible count."""
    groups = "".join(
        f"<optgroup label='Grupa {g}'>"
        + _options([f"Opcja {g}-{i}" for i in range(5)])
        + "</optgroup>"
        for g in range(3)
    )
    await page.set_content(
        f"<body style='margin:0'><select id='s' style='width:220px'>{groups}</select></body>"
    )
    await _inject(page, {"maxVisibleOptions": 8})
    geo = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      const list = api.listFor(s);
      const lr = list.getBoundingClientRect();
      const rows = Array.from(list.querySelectorAll('[data-guidebot-option-index]'));
      return {
        visible: rows.filter((row) => {
          const rr = row.getBoundingClientRect();
          return rr.top >= lr.top - 1 && rr.bottom <= lr.bottom + 1;
        }).length,
        scrolls: list.scrollHeight > list.clientHeight + 1,
        listBottom: lr.bottom,
        innerHeight: window.innerHeight,
      };
    }"""
    )
    assert geo["visible"] >= 8, f"only {geo['visible']} options fit, maxVisibleOptions was 8"
    assert geo["scrolls"] is True, "15 options must still scroll inside the list"
    assert geo["listBottom"] <= geo["innerHeight"] + 0.5
