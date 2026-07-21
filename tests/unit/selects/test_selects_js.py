"""Direct tests of selects.js's public API (no Python ``Selects`` wrapper).

Patterned on ``tests/unit/overlay/test_cursor_js.py``: inject
``window.__guidebot_selects_config`` then evaluate the raw script in a real
Chromium page.

The load-bearing invariant, asserted first and hardest, is that shimming a
``<select>`` leaves the page's DOM structure untouched — the shim is an overlay
appended to ``<body>``, never a wrapper. ``capture_identity`` hashes the whole
composed ancestor chain, so a single inserted wrapper would invalidate every
frozen target under it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from importlib.resources import files

import pytest
from playwright.async_api import Page, Route, async_playwright

SELECTS_JS = files("guidebot_recorder.selects").joinpath("selects.js").read_text("utf-8")

# Must stay strictly below the cursor's own z-index (overlay/cursor.js:18), so
# the synthetic cursor is never painted underneath the option list.
MAX_Z_INDEX = 2147483647


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page()
        try:
            yield pg
        finally:
            await b.close()


async def _inject(page: Page, cfg: dict | None = None) -> None:
    """Install the widget with a short settle window and await the first pass."""
    merged = {"settleMs": 20, **(cfg or {})}
    await page.evaluate(f"window.__guidebot_selects_config = {json.dumps(merged)};")
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")


def _options(labels: list[str]) -> str:
    return "".join(f"<option>{label}</option>" for label in labels)


# A deliberately deep, sibling-sensitive fixture: `select + .hint` is exactly the
# kind of structural CSS selector a wrapper design would break.
NESTED = (
    "<body style='margin:0'><main id='main'><form id='form'>"
    "<div class='row' id='row'>"
    "<label for='s'>Województwo</label>"
    "<select id='s' style='width:220px'>"
    "<option>Mazowieckie</option><option>Lubelskie</option><option>Śląskie</option>"
    "</select>"
    "<span class='hint'>podpowiedź</span>"
    "</div></form></main></body>"
)

_SNAPSHOT_STRUCTURE = """(id) => {
  const el = document.getElementById(id);
  const chain = [];
  for (let a = el.parentElement; a; a = a.parentElement) {
    chain.push([a.tagName.toLowerCase(), a.getAttribute('role') || '', a.id || '']);
  }
  const parent = el.parentElement;
  return {
    chain: chain,
    siblingIndex: Array.prototype.indexOf.call(parent.children, el),
    siblingTags: Array.from(parent.children).map((n) => n.tagName.toLowerCase()),
    nextSiblingClass: el.nextElementSibling ? el.nextElementSibling.className : null,
    structuralSelectorMatches: document.querySelector('select + .hint') !== null,
  };
}"""


async def test_shimming_does_not_touch_the_ancestor_chain(page: Page) -> None:
    """The whole design rests on this: no re-parenting, no wrapper, no move."""
    await page.set_content(NESTED)
    before = await page.evaluate(_SNAPSHOT_STRUCTURE, "s")
    await _inject(page)
    # Open the list too — the overlay must stay out of the page tree even then.
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    after = await page.evaluate(_SNAPSHOT_STRUCTURE, "s")
    assert after == before, "shimming restructured the page DOM"

    # Belt and braces: the shim really did happen, and its elements hang off
    # <body>, not off any ancestor of the select.
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )
    hosts = await page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'[data-guidebot-select-button],[data-guidebot-select-list]'))"
        ".map((n) => n.parentElement.tagName.toLowerCase())"
    )
    assert hosts == ["body", "body"], hosts
    polluted = await page.evaluate(
        "() => { const out = []; "
        "for (let a = document.getElementById('s').parentElement; a; a = a.parentElement) {"
        " for (const at of a.attributes) { if (at.name.startsWith('data-guidebot')) out.push(at.name); } }"
        " return out; }"
    )
    assert polluted == [], polluted


# --- Shadow DOM ------------------------------------------------------------
# The same fixture as NESTED, but behind an open shadow root. `identity_capture`
# and `page_context` both walk *composed* parents, so such a select compiles
# today and must keep working after this branch.
_ATTACH_SHADOW = """() => {
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML =
    "<div class='row' id='row'><label for='s'>Województwo</label>"
    + "<select id='s' style='width:220px'>"
    + "<option>Mazowieckie</option><option>Lubelskie</option><option>Śląskie</option>"
    + "</select><span class='hint'>podpowiedź</span></div>";
}"""

# Mirrors `identity_capture.py`'s `composedParent`: parentElement, else the
# shadow host. This is the chain whose hash freezes every compiled target.
_SNAPSHOT_COMPOSED = """() => {
  const root = document.getElementById('host').shadowRoot;
  const el = root.getElementById('s');
  const composedParent = (node) =>
    node.assignedSlot || node.parentElement ||
    (node.getRootNode() instanceof ShadowRoot ? node.getRootNode().host : null);
  const chain = [];
  for (let a = composedParent(el); a; a = composedParent(a)) {
    chain.push([a.tagName.toLowerCase(), a.getAttribute('role') || '', a.id || '']);
  }
  const parent = el.parentElement;
  return {
    chain: chain,
    siblingIndex: Array.prototype.indexOf.call(parent.children, el),
    siblingTags: Array.from(parent.children).map((n) => n.tagName.toLowerCase()),
    nextSiblingClass: el.nextElementSibling ? el.nextElementSibling.className : null,
    structuralSelectorMatches: root.querySelector('select + .hint') !== null,
    shadowChildCount: root.childNodes.length,
  };
}"""


async def test_a_select_in_an_open_shadow_root_is_shimmed(page: Page) -> None:
    """I3: skipping shadow roots turns a shipped, working case into a hard failure."""
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await page.evaluate(_ATTACH_SHADOW)
    await _inject(page)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('host').shadowRoot.getElementById('s');
      const api = window.__guidebot_selects;
      return {
        isShimmed: api.isShimmed(s),
        marker: s.hasAttribute('data-guidebot-shimmed'),
        buttonHost: api.buttonFor(s) ? api.buttonFor(s).parentElement.tagName.toLowerCase() : null,
        listHost: api.listFor(s) ? api.listFor(s).parentElement.tagName.toLowerCase() : null,
        optionIndex: api.optionIndexFor(s, 'Śląskie'),
      };
    }"""
    )
    assert state == {
        "isShimmed": True,
        "marker": True,
        "buttonHost": "body",
        "listHost": "body",
        "optionIndex": 2,
    }


async def test_shimming_a_shadow_root_select_does_not_touch_its_ancestor_chain(page: Page) -> None:
    """The no-wrapper invariant, verified across the shadow boundary too."""
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await page.evaluate(_ATTACH_SHADOW)
    before = await page.evaluate(_SNAPSHOT_COMPOSED)
    await _inject(page)
    await page.evaluate(
        "() => window.__guidebot_selects.open("
        "document.getElementById('host').shadowRoot.getElementById('s'))"
    )
    after = await page.evaluate(_SNAPSHOT_COMPOSED)
    assert after == before, "shimming restructured the composed ancestor chain"
    polluted = await page.evaluate(
        """() => {
      const root = document.getElementById('host').shadowRoot;
      return Array.from(root.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]')).length;
    }"""
    )
    assert polluted == 0, "the overlay was mounted inside the shadow root"


async def test_mousedown_inside_a_shadow_root_opens_the_list(page: Page) -> None:
    """I3: the listener sees the retargeted host, so it must use `composedPath()`."""
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await page.evaluate(_ATTACH_SHADOW)
    await _inject(page)
    result = await page.evaluate(
        """() => {
      const s = document.getElementById('host').shadowRoot.getElementById('s');
      const ev = new MouseEvent(
        'mousedown', {bubbles: true, cancelable: true, composed: true, button: 0});
      s.dispatchEvent(ev);
      return {
        prevented: ev.defaultPrevented,
        display: getComputedStyle(window.__guidebot_selects.listFor(s)).display,
      };
    }"""
    )
    assert result["prevented"] is True, "native OS popup was not suppressed"
    assert result["display"] != "none", "mousedown inside the shadow root did not open the list"


async def test_a_late_select_in_an_observed_shadow_root_is_shimmed(page: Page) -> None:
    """I3: shadow roots must be observed, not only swept once."""
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await page.evaluate("() => { document.getElementById('host').attachShadow({mode: 'open'}); }")
    await _inject(page)
    await page.evaluate(
        "() => { document.getElementById('host').shadowRoot.innerHTML = "
        '\'<select id="s" style="width:200px"><option>a</option><option>b</option></select>\'; }'
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('[data-guidebot-select-button]').length === 1",
        timeout=3000,
    )


async def test_mutations_in_a_detached_shadow_root_do_not_defer_the_settle_debounce(
    page: Page,
) -> None:
    """M4: an observed root the page has thrown away must stop re-arming the debounce.

    A MutationObserver cannot unobserve a single node, so the detached root keeps
    delivering records. Measured before the fix: a live select was shimmed at
    0.42 s instead of 0.20 s, because every mutation in the dead subtree pushed
    the pending pass out to the deferral ceiling.
    """
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await page.evaluate(
        "() => { document.getElementById('host')"
        ".attachShadow({mode: 'open'}).innerHTML = '<i>x</i>'; }"
    )
    await _inject(page, {"settleMs": 200})
    # The root is observed now; detaching the host does not undo that.
    await page.evaluate(
        """() => {
      const host = document.getElementById('host');
      window.__root = host.shadowRoot;
      host.remove();
    }"""
    )
    elapsed = await page.evaluate(
        """() => new Promise((resolve) => {
      const started = performance.now();
      let n = 0;
      const storm = window.setInterval(() => {
        window.__root.innerHTML = '<i>' + (n += 1) + '</i>';
      }, 16);
      const select = document.createElement('select');
      select.id = 's';
      select.style.width = '220px';
      select.innerHTML = '<option>a</option><option>b</option>';
      document.body.appendChild(select);
      const check = () => {
        if (select.hasAttribute('data-guidebot-shimmed')) {
          window.clearInterval(storm);
          resolve(performance.now() - started);
        } else {
          window.setTimeout(check, 10);
        }
      };
      check();
    })"""
    )
    assert elapsed < 400, f"the detached root deferred the pass to {elapsed:.0f} ms"


async def test_only_mutation_on_the_select_is_the_marker_attribute(page: Page) -> None:
    await page.set_content(NESTED)
    before = await page.evaluate(
        "() => Array.from(document.getElementById('s').attributes)"
        ".map((a) => a.name + '=' + a.value).sort()"
    )
    await _inject(page)
    after = await page.evaluate(
        "() => Array.from(document.getElementById('s').attributes)"
        ".map((a) => a.name + '=' + a.value).sort()"
    )
    added = [a for a in after if a not in before]
    assert [a.split("=")[0] for a in added] == ["data-guidebot-shimmed"], added
    assert [a for a in before if a not in after] == []


async def test_raw_select_gets_button_and_list_addressed_by_uid(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    info = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const uid = s.getAttribute('data-guidebot-shimmed');
      const button = document.querySelector('[data-guidebot-select-button][data-guidebot-for="' + uid + '"]');
      const list = document.querySelector('[data-guidebot-select-list][data-guidebot-for="' + uid + '"]');
      return {
        uid: uid,
        buttonMatches: button === api.buttonFor(s),
        listMatches: list === api.listFor(s),
        buttonAria: button.getAttribute('aria-hidden'),
        listAria: list.getAttribute('aria-hidden'),
        pointerEvents: getComputedStyle(button).pointerEvents,
        buttonZ: Number(getComputedStyle(button).zIndex),
        listZ: Number(getComputedStyle(list).zIndex),
        listDisplay: getComputedStyle(list).display,
        buttonPosition: getComputedStyle(button).position,
        listPosition: getComputedStyle(list).position,
      };
    }"""
    )
    assert info["uid"]
    assert info["buttonMatches"] and info["listMatches"]
    assert info["buttonAria"] == "true" and info["listAria"] == "true"
    # The real <select> must remain Playwright's hit target.
    assert info["pointerEvents"] == "none"
    assert 0 < info["buttonZ"] < MAX_Z_INDEX
    assert 0 < info["listZ"] < MAX_Z_INDEX
    assert info["listDisplay"] == "none", "the list must start closed"
    assert info["buttonPosition"] == "fixed" and info["listPosition"] == "fixed"


async def test_button_is_pinned_to_the_selects_rect(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    delta = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const a = s.getBoundingClientRect();
      const b = window.__guidebot_selects.buttonFor(s).getBoundingClientRect();
      return [Math.abs(a.left - b.left), Math.abs(a.top - b.top),
              Math.abs(a.width - b.width), Math.abs(a.height - b.height)];
    }"""
    )
    assert max(delta) < 1.0, delta


async def test_button_copies_the_selects_own_metrics_and_only_falls_back_when_transparent(
    page: Page,
) -> None:
    """A transparent control would leave the button see-through on camera.

    Only the alpha channel may trigger the fallback: `rgb(255, 255, 0)` is an
    opaque yellow and must be copied verbatim.
    """
    await page.set_content(
        "<body style='margin:0'>"
        "<select id='a' style='width:200px;background-color:rgb(255,255,0);"
        "font-family:Georgia;font-size:19px'><option>x</option></select>"
        "<select id='b' style='width:200px;background-color:rgba(0,0,0,0)'>"
        "<option>x</option></select>"
        "</body>"
    )
    await _inject(page)
    styles = await page.evaluate(
        """() => {
      const api = window.__guidebot_selects;
      const read = (id) => {
        const cs = getComputedStyle(api.buttonFor(document.getElementById(id)));
        return [cs.backgroundColor, cs.fontFamily, cs.fontSize];
      };
      return {a: read('a'), b: read('b')};
    }"""
    )
    assert styles["a"] == ["rgb(255, 255, 0)", "Georgia", "19px"]
    assert styles["b"][0] == "rgb(255, 255, 255)"


LEFT_ALONE = {
    "select2_clipped_to_1px": (
        "<select id='s' style=\"position:absolute;width:1px;height:1px;padding:0;"
        'margin:-1px;overflow:hidden;clip:rect(0 0 0 0)">' + _options(["a", "b"]) + "</select>"
    ),
    "display_none": ("<select id='s' style='display:none'>" + _options(["a", "b"]) + "</select>"),
    "marker_class_select2": (
        "<select id='s' class='select2-hidden-accessible' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "marker_class_tomselect": (
        "<select id='s' class='tomselected' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "marker_class_chosen": (
        "<select id='s' class='chosen-select' style='width:200px;height:30px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "multiple": (
        "<select id='s' multiple style='width:200px;height:80px'>"
        + _options(["a", "b"])
        + "</select>"
    ),
    "size_gt_1": (
        "<select id='s' size='4' style='width:200px;height:80px'>"
        + _options(["a", "b", "c", "d"])
        + "</select>"
    ),
}


@pytest.mark.parametrize("html", list(LEFT_ALONE.values()), ids=list(LEFT_ALONE))
async def test_non_raw_selects_are_left_alone(page: Page, html: str) -> None:
    await page.set_content(f"<body style='margin:0'><div id='host'>{html}</div></body>")
    await _inject(page)
    state = await page.evaluate(
        """() => ({
      overlays: document.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]').length,
      marker: document.getElementById('s').hasAttribute('data-guidebot-shimmed'),
      isShimmed: window.__guidebot_selects.isShimmed(document.getElementById('s')),
      buttonFor: window.__guidebot_selects.buttonFor(document.getElementById('s')),
    })"""
    )
    assert state == {"overlays": 0, "marker": False, "isShimmed": False, "buttonFor": None}


async def test_mousedown_opens_the_list_and_suppresses_the_native_popup(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    result = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const ev = new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0});
      s.dispatchEvent(ev);
      const list = window.__guidebot_selects.listFor(s);
      return {prevented: ev.defaultPrevented, display: getComputedStyle(list).display};
    }"""
    )
    assert result["prevented"] is True, "native OS popup was not suppressed"
    assert result["display"] != "none", "mousedown did not open the DOM list"


@pytest.mark.parametrize("key", ["ArrowDown", "Enter", " "])
async def test_keyboard_opens_the_list(page: Page, key: str) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    result = await page.evaluate(
        """(key) => {
      const s = document.getElementById('s');
      const ev = new KeyboardEvent('keydown', {key: key, bubbles: true, cancelable: true});
      s.dispatchEvent(ev);
      return {
        prevented: ev.defaultPrevented,
        display: getComputedStyle(window.__guidebot_selects.listFor(s)).display,
      };
    }""",
        key,
    )
    assert result["display"] != "none"
    # Same reason as mousedown: unprevented, the key would also open Chromium's
    # native, un-recordable popup (and ' ' would scroll the page).
    assert result["prevented"] is True, "the native key handling was not suppressed"


async def test_real_click_on_the_select_unfurls_the_list(page: Page) -> None:
    """End-to-end proof that the shim button does not steal the hit target."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.click("#s")
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    list_selector = f'[data-guidebot-select-list][data-guidebot-for="{uid}"]'
    await page.wait_for_selector(list_selector, state="visible", timeout=3000)


# Two shimmed selects on one page — the normal case, not an edge case. The bare
# `[data-guidebot-option-index="N"]` selector then matches one row per select.
NESTED_AND_A_SECOND_SELECT = NESTED.replace(
    "</main></body>",
    "</main><select id='other' style='width:220px'>"
    "<option>Mazowieckie</option><option>Lubelskie</option><option>Śląskie</option>"
    "</select></body>",
)


def option_selector(uid: str, index: int) -> str:
    """The documented, uid-scoped way to address one shimmed select's option row.

    The bare attribute selector is ambiguous the moment a page has two shimmed
    selects, and Playwright's strict mode rejects it.
    """
    return (
        f'[data-guidebot-select-list][data-guidebot-for="{uid}"] '
        f'[data-guidebot-option-index="{index}"]'
    )


async def test_option_rows_must_be_addressed_scoped_to_their_own_select(page: Page) -> None:
    """M6: the bare selector is a Playwright strict-mode violation."""
    await page.set_content(NESTED_AND_A_SECOND_SELECT)
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    assert await page.locator('[data-guidebot-option-index="2"]').count() == 2
    with pytest.raises(Exception, match="strict mode violation"):
        await page.locator('[data-guidebot-option-index="2"]').click(timeout=1500)
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    assert await page.locator(option_selector(uid, 2)).count() == 1
    await page.locator(option_selector(uid, 2)).click()
    assert await page.evaluate("() => document.getElementById('s').selectedIndex") == 2


async def test_choosing_an_option_sets_value_and_fires_input_and_change(page: Page) -> None:
    await page.set_content(NESTED_AND_A_SECOND_SELECT)
    await _inject(page)
    await page.evaluate(
        """() => {
      window.__events = [];
      const s = document.getElementById('s');
      for (const type of ['input', 'change']) {
        s.addEventListener(type, (e) => window.__events.push([type, e.bubbles]));
      }
      window.__guidebot_selects.open(s);
    }"""
    )
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.click(option_selector(uid, 2))
    state = await page.evaluate(
        """() => ({
      value: document.getElementById('s').value,
      index: document.getElementById('s').selectedIndex,
      otherIndex: document.getElementById('other').selectedIndex,
      events: window.__events,
      display: getComputedStyle(
        window.__guidebot_selects.listFor(document.getElementById('s'))).display,
    })"""
    )
    assert state["index"] == 2
    assert state["value"] == "Śląskie"
    assert state["otherIndex"] == 0, "the click leaked into the other shimmed select"
    assert state["events"] == [["input", True], ["change", True]]
    assert state["display"] == "none", "choosing an option must close the list"


async def test_rechoosing_the_current_option_fires_nothing(page: Page) -> None:
    """Native selects stay silent when the value does not change (spec §4)."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      s.selectedIndex = 1;
      window.__events = [];
      for (const type of ['input', 'change']) {
        s.addEventListener(type, () => window.__events.push(type));
      }
      window.__guidebot_selects.open(s);
    }"""
    )
    uid = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.click(option_selector(uid, 1))
    state = await page.evaluate(
        "() => ({events: window.__events, index: document.getElementById('s').selectedIndex})"
    )
    assert state["events"] == []
    assert state["index"] == 1


async def test_list_opens_downward_and_clamps_max_height_with_internal_scrolling(
    page: Page,
) -> None:
    labels = [f"Opcja {i}" for i in range(30)]
    await page.set_content(
        "<body style='margin:0'><div style='height:400px'></div>"
        f"<select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    geo = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      window.__guidebot_selects.open(s);
      const list = window.__guidebot_selects.listFor(s);
      const sr = s.getBoundingClientRect();
      const lr = list.getBoundingClientRect();
      return {
        selectBottom: sr.bottom,
        listTop: lr.top,
        listBottom: lr.bottom,
        maxHeight: Number.parseFloat(getComputedStyle(list).maxHeight),
        overflowY: getComputedStyle(list).overflowY,
        scrollHeight: list.scrollHeight,
        clientHeight: list.clientHeight,
        innerHeight: window.innerHeight,
      };
    }"""
    )
    assert geo["listTop"] >= geo["selectBottom"] - 0.5, "the list flipped upward"
    assert geo["maxHeight"] >= 120
    assert geo["listBottom"] <= geo["innerHeight"] + 0.5, "the list spilled past the frame"
    assert geo["overflowY"] in ("auto", "scroll")
    assert geo["scrollHeight"] > geo["clientHeight"] + 1, "the list did not clamp"


async def test_list_never_flips_upward_and_keeps_a_floor_height(page: Page) -> None:
    """Right at the bottom edge there is no room; the floor wins, not a flip."""
    labels = [f"Opcja {i}" for i in range(30)]
    await page.set_content(
        "<body style='margin:0'><div style='height:690px'></div>"
        f"<select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    geo = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      window.__guidebot_selects.open(s);
      const list = window.__guidebot_selects.listFor(s);
      return {
        selectBottom: s.getBoundingClientRect().bottom,
        listTop: list.getBoundingClientRect().top,
        maxHeight: Number.parseFloat(getComputedStyle(list).maxHeight),
      };
    }"""
    )
    assert geo["listTop"] >= geo["selectBottom"] - 0.5, "the list flipped upward"
    assert abs(geo["maxHeight"] - 120) < 1.0, geo["maxHeight"]


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

    # `max-height` alone proves nothing: `div {height:120px!important}` above wins
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


async def test_observer_shims_a_late_select(page: Page) -> None:
    await page.set_content("<body style='margin:0'><div id='host'></div></body>")
    await _inject(page)
    assert (
        await page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-button]').length"
        )
        == 0
    )
    await page.evaluate(
        "() => { document.getElementById('host').innerHTML = "
        '\'<select id="s" style="width:200px"><option>a</option><option>b</option></select>\'; }'
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('[data-guidebot-select-button]').length === 1",
        timeout=3000,
    )


_ROW_TEXTS = """() => {
  const s = document.getElementById('s');
  const api = window.__guidebot_selects;
  return {
    rows: Array.from(api.listFor(s).querySelectorAll('[data-guidebot-option-index]'))
      .map((n) => n.textContent),
    button: api.buttonFor(s).textContent.trim(),
  };
}"""


async def test_option_rows_follow_a_replaced_option_set_while_the_list_is_closed(
    page: Page,
) -> None:
    """M7: rows were rebuilt only in `open()`, so they read stale text until then."""
    await page.set_content(NESTED)
    await _inject(page)
    assert (await page.evaluate(_ROW_TEXTS))["rows"] == ["Mazowieckie", "Lubelskie", "Śląskie"]
    await page.evaluate(
        "() => { document.getElementById('s').innerHTML = "
        "'<option>Alfa</option><option>Beta</option>'; }"
    )
    await page.wait_for_function(
        "() => Array.from(window.__guidebot_selects"
        ".listFor(document.getElementById('s'))"
        ".querySelectorAll('[data-guidebot-option-index]'))"
        ".map((n) => n.textContent).join('|') === 'Alfa|Beta'",
        timeout=3000,
    )
    assert (await page.evaluate(_ROW_TEXTS))["button"] == "Alfa"


async def test_an_option_set_that_only_moves_a_separator_still_rebuilds_the_rows(
    page: Page,
) -> None:
    """M8: joining the signature parts with "" lets distinct option sets collide.

    ``["a", "b"]`` fingerprints as ``o:a`` + ``o:b`` and the single option
    ``ao:b`` as ``o:ao:b`` — the same string, so the rows are never rebuilt and
    the list keeps showing options the select no longer has.
    """
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<option>a</option><option>b</option></select></body>"
    )
    await _inject(page)
    assert (await page.evaluate(_ROW_TEXTS))["rows"] == ["a", "b"]
    await page.evaluate(
        "() => { document.getElementById('s').innerHTML = '<option>ao:b</option>'; }"
    )
    await page.wait_for_function(
        "() => Array.from(window.__guidebot_selects"
        ".listFor(document.getElementById('s'))"
        ".querySelectorAll('[data-guidebot-option-index]'))"
        ".map((n) => n.textContent).join('|') === 'ao:b'",
        timeout=3000,
    )


async def test_rebuilding_rows_keeps_an_open_lists_highlight(page: Page) -> None:
    """The rebuild must not drop the active row out from under an open list.

    The highlight is moved *away* from the selection first: a rebuild that
    re-applies `selectedIndex` looks correct as long as the two agree, so only an
    arrow-key highlight that has left the selection behind can catch it.
    """
    await page.set_content(NESTED)
    await _inject(page)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      s.selectedIndex = 0;
      window.__guidebot_selects.open(s);
      for (let i = 0; i < 2; i += 1) {
        s.dispatchEvent(new KeyboardEvent(
          'keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
      }
      const list = window.__guidebot_selects.listFor(s);
      const row = list.querySelector('[data-guidebot-option-active]');
      return {
        active: row ? row.getAttribute('data-guidebot-option-index') : null,
        selectedIndex: s.selectedIndex,
      };
    }"""
    )
    assert state == {"active": "2", "selectedIndex": 0}, "the fixture never moved the highlight"
    await page.evaluate(
        "() => { document.getElementById('s').insertAdjacentHTML("
        "'beforeend', '<option>Pomorskie</option>'); }"
    )
    await page.wait_for_function(
        "() => window.__guidebot_selects.listFor(document.getElementById('s'))"
        ".querySelectorAll('[data-guidebot-option-index]').length === 4",
        timeout=3000,
    )
    after = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const row = window.__guidebot_selects.listFor(s)
        .querySelector('[data-guidebot-option-active]');
      return {
        active: row ? row.getAttribute('data-guidebot-option-index') : null,
        selectedIndex: s.selectedIndex,
      };
    }"""
    )
    assert after == {"active": "2", "selectedIndex": 0}


async def test_observer_unshims_a_select_that_gains_a_marker_class(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    assert (
        await page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-button]').length"
        )
        == 1
    )
    await page.evaluate(
        "() => document.getElementById('s').classList.add('select2-hidden-accessible')"
    )
    await page.wait_for_function(
        "() => document.querySelectorAll("
        "'[data-guidebot-select-button],[data-guidebot-select-list]').length === 0",
        timeout=3000,
    )
    left = await page.evaluate(
        "() => document.getElementById('s').hasAttribute('data-guidebot-shimmed')"
    )
    assert left is False, "the marker attribute survived unshimming"


async def test_overlays_are_reattached_when_an_spa_replaces_the_body(page: Page) -> None:
    """The select survives the swap; the overlays go with the old <body>."""
    await page.set_content(NESTED)
    await _inject(page)
    uid_before = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.evaluate(
        """() => {
      const select = document.getElementById('s');
      const body = document.createElement('body');
      body.setAttribute('style', 'margin:0');
      body.appendChild(select);
      document.documentElement.replaceChild(body, document.body);
    }"""
    )
    assert (
        await page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-button]').length"
        )
        == 0
    ), "the fixture did not actually strip the overlays"
    await page.wait_for_function(
        "() => document.querySelectorAll('[data-guidebot-select-button]').length === 1"
        " && document.querySelectorAll('[data-guidebot-select-list]').length === 1",
        timeout=3000,
    )
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      return {
        uid: s.getAttribute('data-guidebot-shimmed'),
        isShimmed: api.isShimmed(s),
        buttonHost: api.buttonFor(s).parentElement.tagName.toLowerCase(),
      };
    }"""
    )
    assert state == {"uid": uid_before, "isShimmed": True, "buttonHost": "body"}


async def test_observer_unshims_a_select_that_loses_its_box(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate("() => { document.getElementById('s').style.display = 'none'; }")
    await page.wait_for_function(
        "() => document.querySelectorAll("
        "'[data-guidebot-select-button],[data-guidebot-select-list]').length === 0",
        timeout=3000,
    )


async def test_observer_drops_the_overlay_of_a_removed_select(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate("() => document.getElementById('s').remove()")
    await page.wait_for_function(
        "() => document.querySelectorAll("
        "'[data-guidebot-select-button],[data-guidebot-select-list]').length === 0",
        timeout=3000,
    )


_TWO_FRAMES = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"

_OVERLAY_GEOMETRY = """() => {
  const list = window.__list;
  const button = window.__button;
  const lr = list.getBoundingClientRect();
  return {
    listDisplay: getComputedStyle(list).display,
    buttonDisplay: getComputedStyle(button).display,
    listRect: [lr.left, lr.top, lr.width, lr.height],
    isOpen: window.__guidebot_selects.isOpen(list),
  };
}"""


async def test_removing_a_select_hides_its_open_list_within_a_frame(page: Page) -> None:
    """I2: a detached select must not leave a ghost dropdown on camera.

    `unshim` only runs from the debounced `classify()`; with the shipped
    `settle_ms=1000` that leaves the list painted — and pinned to the detached
    select's zero rect, i.e. a sliver in the corner — for a whole second.
    """
    await page.set_content(NESTED)
    await _inject(page, {"settleMs": 1000})
    await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      window.__list = api.listFor(s);
      window.__button = api.buttonFor(s);
    }"""
    )
    assert (await page.evaluate(_OVERLAY_GEOMETRY))["listDisplay"] == "block"
    await page.evaluate("() => document.getElementById('s').remove()")
    await page.evaluate(_TWO_FRAMES)
    state = await page.evaluate(_OVERLAY_GEOMETRY)
    assert state["listDisplay"] == "none", "the removed select left its list on camera"
    assert state["buttonDisplay"] == "none", "the removed select left its button on camera"
    assert state["isOpen"] is False


async def test_a_select_that_collapses_to_nothing_closes_its_list(page: Page) -> None:
    """I2: same story for a select a widget library hides while the list is open."""
    await page.set_content(NESTED)
    await _inject(page, {"settleMs": 1000})
    await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      window.__list = api.listFor(s);
      window.__button = api.buttonFor(s);
      s.style.display = 'none';
    }"""
    )
    await page.evaluate(_TWO_FRAMES)
    state = await page.evaluate(_OVERLAY_GEOMETRY)
    assert state["listDisplay"] == "none"
    assert state["buttonDisplay"] == "none"
    assert state["isOpen"] is False


async def test_optgroups_are_headings_and_disabled_options_are_not_clickable(page: Page) -> None:
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<optgroup label='Północ'><option>Gdańsk</option><option disabled>Olsztyn</option></optgroup>"
        "<optgroup label='Południe'><option>Kraków</option></optgroup>"
        "</select></body>"
    )
    await _inject(page)
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    shape = await page.evaluate(
        """() => {
      const list = window.__guidebot_selects.listFor(document.getElementById('s'));
      return {
        groups: Array.from(list.querySelectorAll('[data-guidebot-optgroup]')).map((n) => n.textContent),
        groupsClickable: Array.from(list.querySelectorAll('[data-guidebot-optgroup]'))
          .some((n) => n.hasAttribute('data-guidebot-option-index')),
        rows: Array.from(list.querySelectorAll('[data-guidebot-option-index]'))
          .map((n) => [n.getAttribute('data-guidebot-option-index'), n.textContent]),
        disabledDimmed: Number.parseFloat(getComputedStyle(
          list.querySelector('[data-guidebot-option-index="1"]')).opacity) < 1,
      };
    }"""
    )
    assert shape["groups"] == ["Północ", "Południe"]
    assert shape["groupsClickable"] is False
    assert shape["rows"] == [["0", "Gdańsk"], ["1", "Olsztyn"], ["2", "Kraków"]]
    assert shape["disabledDimmed"] is True

    # Clicking the disabled row changes nothing and does not close the list.
    await page.evaluate(
        """() => {
      window.__events = [];
      document.getElementById('s').addEventListener('change', () => window.__events.push('change'));
      window.__guidebot_selects.listFor(document.getElementById('s'))
        .querySelector('[data-guidebot-option-index="1"]')
        .dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }"""
    )
    state = await page.evaluate(
        "() => ({events: window.__events, index: document.getElementById('s').selectedIndex})"
    )
    assert state == {"events": [], "index": 0}


async def test_option_index_for_normalizes_whitespace_and_falls_back_to_case_insensitive(
    page: Page,
) -> None:
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<option>Pierwszy</option>"
        "<option>  Drugi\n   wybór </option>"
        "<option>Trzeci</option>"
        "</select></body>"
    )
    await _inject(page)
    found = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      return [
        api.optionIndexFor(s, 'Drugi wybór'),
        api.optionIndexFor(s, '   Drugi    wybór  '),
        api.optionIndexFor(s, 'drugi WYBÓR'),
        api.optionIndexFor(s, 'Pierwszy'),
        api.optionIndexFor(s, 'Nie ma takiej'),
      ];
    }"""
    )
    assert found == [1, 1, 1, 0, -1]


async def test_the_label_attribute_wins_over_the_option_text_like_select_option_does(
    page: Page,
) -> None:
    """M8: compile drives via `locator.select_option(label=…)`, which reads `option.label`.

    Resolving the row text or the index off `textContent` would make compile and
    render disagree about the same option, and put text on camera that the native
    control never shows.
    """
    await page.set_content(
        "<body style='margin:0'><select id='s' style='width:220px'>"
        "<option>Pierwszy</option>"
        "<option label='Krótko'>bardzo długi tekst opcji</option>"
        "</select></body>"
    )
    await _inject(page)
    # Exactly what compile does — the reference behaviour render must match.
    await page.select_option("#s", label="Krótko")
    await page.evaluate(_TWO_FRAMES)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      const list = api.listFor(s);
      return {
        selectedIndex: s.selectedIndex,
        indexForLabel: api.optionIndexFor(s, 'Krótko'),
        indexForText: api.optionIndexFor(s, 'bardzo długi tekst opcji'),
        rowText: list.querySelector('[data-guidebot-option-index="1"]').textContent,
        buttonText: api.buttonFor(s).textContent.trim(),
      };
    }"""
    )
    assert state["selectedIndex"] == 1
    assert state["indexForLabel"] == 1, "optionIndexFor ignored the label attribute"
    assert state["indexForText"] == -1, "textContent resolved an option select_option would not"
    assert state["rowText"] == "Krótko", "the on-camera row text differs from the native control"
    assert state["buttonText"] == "Krótko"


async def test_scroll_option_into_view_brings_a_far_row_into_the_list_box(page: Page) -> None:
    labels = [f"Opcja {i}" for i in range(40)]
    await page.set_content(
        f"<body style='margin:0'><select id='s' style='width:220px'>{_options(labels)}</select></body>"
    )
    await _inject(page)
    state = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      api.open(s);
      const list = api.listFor(s);
      const before = list.scrollTop;
      api.scrollOptionIntoView(s, 35);
      const row = list.querySelector('[data-guidebot-option-index="35"]');
      const lr = list.getBoundingClientRect();
      const rr = row.getBoundingClientRect();
      return {before: before, after: list.scrollTop,
              inside: rr.top >= lr.top - 1 && rr.bottom <= lr.bottom + 1};
    }"""
    )
    assert state["before"] == 0
    assert state["after"] > 0
    assert state["inside"] is True


async def test_close_hides_the_list_and_open_is_idempotent(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    states = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const list = api.listFor(s);
      const read = () => getComputedStyle(list).display;
      const out = [read()];
      api.open(s); out.push(read());
      api.open(s); out.push(read());
      api.close(s); out.push(read());
      api.close(s); out.push(read());
      return out;
    }"""
    )
    assert states == ["none", "block", "block", "none", "none"]


async def test_native_mode_installs_no_shim_but_still_resolves_ready(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page, {"mode": "native"})
    state = await page.evaluate(
        """() => ({
      overlays: document.querySelectorAll(
        '[data-guidebot-select-button],[data-guidebot-select-list]').length,
      isShimmed: window.__guidebot_selects.isShimmed(document.getElementById('s')),
      marker: document.getElementById('s').hasAttribute('data-guidebot-shimmed'),
    })"""
    )
    assert state == {"overlays": 0, "isShimmed": False, "marker": False}


async def test_native_mode_offers_the_whole_api_as_no_ops(page: Page) -> None:
    """compile/render call this surface unconditionally — no `mode` branch of their own."""
    await page.set_content(NESTED)
    await _inject(page, {"mode": "native"})
    surface = await page.evaluate(
        """() => {
      const s = document.getElementById('s');
      const api = window.__guidebot_selects;
      const missing = [
        'ready', 'isShimmed', 'buttonFor', 'listFor', 'isOpen', 'open', 'close',
        'optionIndexFor', 'scrollOptionIntoView', 'refresh',
      ].filter((name) => api[name] === undefined);
      api.open(s);
      api.close(s);
      api.refresh();
      api.scrollOptionIntoView(s, 1);
      return {
        missing: missing,
        readyIsShared: api.ready === window.__guidebot_selects_ready,
        isShimmed: api.isShimmed(s),
        buttonFor: api.buttonFor(s),
        listFor: api.listFor(s),
        isOpen: api.isOpen(s),
        optionIndexFor: api.optionIndexFor(s, 'Mazowieckie'),
        selectedIndex: s.selectedIndex,
      };
    }"""
    )
    assert surface == {
        "missing": [],
        "readyIsShared": True,
        "isShimmed": False,
        "buttonFor": None,
        "listFor": None,
        "isOpen": False,
        "optionIndexFor": -1,
        "selectedIndex": 0,
    }


async def test_the_role_gate_does_not_depend_on_the_registration_order(page: Page) -> None:
    """G1: `chrome.js` shadows `top` only where the origin is never the shell's.

    So the shim installs identically whether it ran before or after that
    shadowing — the ordering comment used to claim otherwise.
    """
    await page.set_content("<body style='margin:0'><iframe id='f' srcdoc=\"\"></iframe></body>")
    await page.evaluate(
        """() => new Promise((resolve) => {
      const frame = document.getElementById('f');
      frame.addEventListener('load', resolve, {once: true});
      frame.srcdoc = "<body style='margin:0'><select id='s' style='width:200px'>"
        + "<option>a</option><option>b</option></select></body>";
    })"""
    )
    child = page.frames[1]
    # Exactly what chrome.js:18-27 does inside a framed document, run *first* —
    # including its tolerance for a `top` that cannot be redefined at all.
    shadowed = await child.evaluate(
        """() => {
      try {
        const selfWindow = window;
        Object.defineProperty(window, 'top', {configurable: true, get: () => selfWindow});
      } catch (_error) {
        /* `top` is LegacyUnforgeable here; chrome.js swallows this too */
      }
      return window === window.top;
    }"""
    )
    await child.evaluate('window.__guidebot_selects_config = {"settleMs": 20};')
    await child.evaluate(SELECTS_JS)
    await child.evaluate("window.__guidebot_selects.ready")
    assert await child.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    ), f"the shim skipped a framed site document (top shadowed: {shadowed})"


async def test_script_bails_out_in_the_shell_document(page: Page) -> None:
    """The shell holds no page content; shimming there would be nonsense."""

    async def handler(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><body><select id='s' style='width:200px'>"
            "<option>a</option><option>b</option></select></body></html>",
        )

    await page.route("https://guidebot.shell/**", handler)
    await page.goto("https://guidebot.shell/")
    assert await page.evaluate("() => window.location.origin") == "https://guidebot.shell"
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 20};')
    await page.evaluate(SELECTS_JS)
    assert await page.evaluate("() => window.__guidebot_selects === undefined")
    await page.wait_for_timeout(200)
    assert (
        await page.evaluate(
            "() => document.querySelectorAll("
            "'[data-guidebot-select-button],[data-guidebot-select-list]').length"
        )
        == 0
    )


async def test_reinjection_does_not_duplicate_overlays(page: Page) -> None:
    """add_init_script + an explicit evaluate must not stack two shims."""
    await page.set_content(NESTED)
    await _inject(page)
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")
    await page.wait_for_timeout(120)
    counts = await page.evaluate(
        """() => ({
      buttons: document.querySelectorAll('[data-guidebot-select-button]').length,
      lists: document.querySelectorAll('[data-guidebot-select-list]').length,
    })"""
    )
    assert counts == {"buttons": 1, "lists": 1}


async def test_reinjection_rearms_the_debounce_instead_of_classifying_immediately(
    page: Page,
) -> None:
    """I5: `Overlay.install`'s add_init_script + evaluate idiom must not race.

    A second injection at t≈0 that classified straight away would beat select2
    (or any other widget library) to the select — the exact race `settle_ms`
    exists to prevent.
    """
    await page.set_content(
        "<body style='margin:0'>"
        f"<select id='s' style='width:220px'>{_options(['a', 'b'])}</select></body>"
    )
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 500};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate(SELECTS_JS)  # the re-injection guard path
    immediate = await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    )
    assert immediate is False, "re-injection classified without waiting for settle_ms"
    await page.evaluate("window.__guidebot_selects.ready")
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    ), "the settle window elapsed without classifying"


def test_is_relevant_does_not_guard_against_undelivered_mutations() -> None:
    """M9: the marker attribute is not in `attributeFilter`, so skipping it was dead code.

    A source check rather than a behavioural one: the branch could never run, so
    only the source can say whether it came back.
    """
    filtered = re.search(r"attributeFilter: \[([^\]]*)\]", SELECTS_JS)
    assert filtered is not None, "attributeFilter moved; update this test"
    names = [part.strip().strip('"') for part in filtered.group(1).split(",") if part.strip()]
    assert "data-guidebot-shimmed" not in names, names
    body = re.search(r"function isRelevant\(records\) \{(.*?)\n  \}\n", SELECTS_JS, re.S)
    assert body is not None, "isRelevant moved; update this test"
    assert "MARKER_ATTRIBUTE" not in body.group(1), (
        "isRelevant still guards against a mutation the observer never delivers"
    )


async def test_the_observer_does_not_react_to_the_shims_own_mutations(page: Page) -> None:
    """The overlay writes styles every frame; that must not re-arm the debounce.

    A feedback loop here would re-classify (and churn shims) forever, burning
    CPU during every render.
    """
    await page.set_content(NESTED)
    await _inject(page)
    first = await page.get_attribute("#s", "data-guidebot-shimmed")
    await page.evaluate("() => window.__guidebot_selects.open(document.getElementById('s'))")
    await page.wait_for_timeout(400)
    second = await page.get_attribute("#s", "data-guidebot-shimmed")
    assert second == first, "the shim was torn down and rebuilt by its own mutations"
    counts = await page.evaluate(
        """() => ({
      buttons: document.querySelectorAll('[data-guidebot-select-button]').length,
      lists: document.querySelectorAll('[data-guidebot-select-list]').length,
    })"""
    )
    assert counts == {"buttons": 1, "lists": 1}


# A page that writes an inline style on every animation frame. Not a hostile
# construct: in popup documents `cursor.js` writes `left`/`top` every frame for
# the whole length of a glide, and `chrome.js` mutates its bar every 24 ms while
# a URL is being typed. Both re-arm the shim's settle debounce.
_EVERY_FRAME_STYLE_STORM = """() => {
  const el = document.getElementById('storm');
  let n = 0;
  const step = () => {
    el.style.transform = 'translateX(' + (n++ % 7) + 'px)';
    window.requestAnimationFrame(step);
  };
  window.requestAnimationFrame(step);
}"""

_WATCH_READY = """() => {
  window.__ready = false;
  window.__guidebot_selects.ready.then(() => {
    window.__ready = true;
  });
}"""


async def test_ready_settles_even_when_the_page_mutates_every_frame(page: Page) -> None:
    """C1: an every-frame style write must not starve the first pass forever.

    `ready` never settling is not a cosmetic failure: `Selects.wait_ready` awaits
    that promise, so compile and render would hang indefinitely.
    """
    await page.set_content(
        "<body style='margin:0'><div id='storm'>x</div>"
        f"<select id='s' style='width:220px'>{_options(['a', 'b'])}</select></body>"
    )
    await page.evaluate(_EVERY_FRAME_STYLE_STORM)
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 200};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate(_WATCH_READY)
    await page.wait_for_function("() => window.__ready === true", timeout=5000)
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))"
    ), "the first pass resolved `ready` without ever shimming anything"


async def test_a_throwing_classification_pass_still_resolves_ready(page: Page) -> None:
    """M5: without `try/finally` a throw leaves `ready` pending for good.

    The guaranteed timer is cleared only at the end of `classify()`, so once it
    has fired, a pass that throws every time takes `markReady` down with it — and
    `Selects.wait_ready` blocks compile and render on exactly that promise.
    """
    await page.set_content(NESTED)
    await page.evaluate(
        """() => {
      window.__guidebot_selects_config = {settleMs: 20};
      const original = Element.prototype.appendChild;
      Element.prototype.appendChild = function (node) {
        if (node && node.nodeType === 1 && node.hasAttribute('data-guidebot-select-button')) {
          throw new Error('a page hook threw while the shim was mounting');
        }
        return original.call(this, node);
      };
    }"""
    )
    await page.evaluate(SELECTS_JS)
    outcome = await page.evaluate(
        """() => Promise.race([
      window.__guidebot_selects.ready.then(() => 'ready'),
      new Promise((r) => window.setTimeout(() => r('never settled'), 2000)),
    ])"""
    )
    assert outcome == "ready"


async def test_a_late_select_is_shimmed_while_the_page_mutates_every_frame(page: Page) -> None:
    """C1: the observer's debounce needs a cap, not just an uncancellable first pass.

    The storm starts *after* `ready`, so only the maximum-deferral cap can keep
    this classification pass from being postponed forever.
    """
    await page.set_content("<body style='margin:0'><div id='storm'>x</div></body>")
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 200};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")
    await page.evaluate(_EVERY_FRAME_STYLE_STORM)
    await page.evaluate(
        "() => { const s = document.createElement('select');"
        " s.id = 's'; s.style.width = '220px';"
        " s.innerHTML = '<option>a</option><option>b</option>';"
        " document.body.appendChild(s); }"
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('[data-guidebot-select-button]').length === 1",
        timeout=5000,
    )


# The same storm, but on the macrotask queue instead of the frame clock. A
# `clearTimeout`+`setTimeout` ceiling can never win against this: the re-armed
# timer is always queued *behind* the storm's own already-pending one, so the
# pass is postponed for as long as the page keeps mutating.
_EVERY_TASK_STYLE_STORM = """() => {
  const el = document.getElementById('storm');
  let n = 0;
  const step = () => {
    el.style.transform = 'translateX(' + (n++ % 7) + 'px)';
    window.setTimeout(step, 0);
  };
  window.setTimeout(step, 0);
}"""


async def test_the_deferral_ceiling_survives_a_zero_delay_timer_storm(page: Page) -> None:
    """M3: the cap must be its own uncancellable deadline, not a re-armed debounce.

    Measured before the fix: a select appended during a `setTimeout(fn, 0)` storm
    was still unshimmed after 5 s, even though the ceiling is 3 × 200 ms.
    """
    await page.set_content("<body style='margin:0'><div id='storm'>x</div></body>")
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 200};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")
    await page.evaluate(_EVERY_TASK_STYLE_STORM)
    await page.evaluate(
        "() => { const s = document.createElement('select');"
        " s.id = 's'; s.style.width = '220px';"
        " s.innerHTML = '<option>a</option><option>b</option>';"
        " document.body.appendChild(s); }"
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('[data-guidebot-select-button]').length === 1",
        timeout=3000,
    )


async def test_button_shows_the_selected_label_and_follows_external_changes(page: Page) -> None:
    await page.set_content(NESTED)
    await _inject(page)
    first = await page.evaluate(
        "() => window.__guidebot_selects.buttonFor(document.getElementById('s')).textContent.trim()"
    )
    assert first == "Mazowieckie"
    await page.evaluate("() => { document.getElementById('s').selectedIndex = 2; }")
    await page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )
    second = await page.evaluate(
        "() => window.__guidebot_selects.buttonFor(document.getElementById('s')).textContent.trim()"
    )
    assert second == "Śląskie"
