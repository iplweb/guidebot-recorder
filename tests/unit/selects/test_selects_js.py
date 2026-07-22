"""Direct tests of selects.js's public API (no Python ``Selects`` wrapper).

Patterned on ``tests/unit/overlay/test_cursor_js.py``: inject
``window.__guidebot_selects_config`` then evaluate the raw script in a real
Chromium page.

The load-bearing invariant, asserted first and hardest, is that shimming a
``<select>`` leaves the page's DOM structure untouched — the shim is an overlay
appended to ``<body>``, never a wrapper. ``capture_identity`` hashes the whole
composed ancestor chain, so a single inserted wrapper would invalidate every
frozen target under it.

**"First" is load-bearing, and it is mechanical.** This file used to hold the
whole suite; the 600-line limit split it into seven. The structural invariant
kept the unsuffixed name on purpose: pytest collects a directory alphabetically
and ``.`` (0x2E) sorts before ``_`` (0x5F), so ``test_selects_js.py`` is still
collected before ``test_selects_js_geometry.py`` and its five siblings. Renaming
this file — or giving it a suffix of its own — silently demotes the invariant.
The rest of the family, in collection order:

* ``test_selects_js_geometry.py`` — hostile page CSS, overlay style pinning,
  re-pinning on scroll, ``maxVisibleOptions``.
* ``test_selects_js_native.py`` — ``pinNative`` and the ``mode: native`` no-ops.
* ``test_selects_js_observer.py`` — the ``MutationObserver``: late selects,
  replaced option sets, unshimming, SPA body swaps, the role gate, re-injection.
* ``test_selects_js_options.py`` — optgroups, disabled rows, ``optionIndexFor``,
  ``scrollOptionIntoView``.
* ``test_selects_js_readiness.py`` — ``ready``/``settled()`` under mutation
  storms and a document rewrite.
* ``test_selects_js_shim.py`` — the shim itself: button/list/uid, pinning,
  copied metrics, opening, choosing, list height.

Everything they share — ``SELECTS_JS``, the browser session, ``_inject``,
``_options``, ``NESTED`` — lives in ``_selects_js_helpers.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import NESTED, _inject, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


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
# The same markup as `NESTED` (defined in `_selects_js_helpers.py`), but behind
# an open shadow root, and asserted the same way as
# `test_shimming_does_not_touch_the_ancestor_chain` above. `identity_capture`
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
    the pending pass out to the deferral ceiling (the ceiling itself is asserted
    by `test_the_deferral_ceiling_survives_a_zero_delay_timer_storm` in
    `test_selects_js_readiness.py`; here it is the symptom, not the subject).
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
