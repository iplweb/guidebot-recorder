"""The ``MutationObserver``: keeping the shim in step with a page that moves.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map. Shimming is not a one-shot sweep: selects appear late, gain marker classes,
lose their box, get removed, have their option sets replaced, and an SPA can
swap the whole ``<body>`` out. This file also holds the role gate (which
documents refuse the shim at all) and the re-injection guard.

The two shadow-root observer cases live in ``test_selects_js.py`` instead, with
the rest of the shadow-DOM block.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, Route

from ._selects_js_helpers import (
    _TWO_FRAMES,
    NESTED,
    SELECTS_JS,
    _inject,
    _options,
    selects_page,
)


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


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
