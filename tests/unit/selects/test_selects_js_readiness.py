"""``ready`` and ``settled()``: the barriers compile and render block on.

Split out of ``test_selects_js.py``; see that file's docstring for the family
map. ``Selects.wait_ready`` awaits ``ready`` and every ``select:`` step awaits
``settled()``, so a barrier that never resolves is not a cosmetic failure — it
hangs the whole run. The tests here are all shapes of "the page never goes
quiet": a style write on every animation frame, a ``setTimeout(fn, 0)`` storm, a
page hook that throws, and a ``document.open()`` that swaps the observed root
out from under the observer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page

from ._selects_js_helpers import NESTED, SELECTS_JS, _inject, _options, selects_page


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with selects_page() as pg:
        yield pg


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


# --- `settled()`: the barrier for the pass a mutation owes -------------------

_APPEND_SELECT = """() => {
  const s = document.createElement('select');
  s.id = 'late';
  s.style.width = '220px';
  s.innerHTML = '<option>a</option><option>b</option>';
  document.body.appendChild(s);
}"""

#: Ask for the barrier and report what the DOM looked like when it resolved —
#: the assertion is about *ordering*, so it has to be sampled in the page, not
#: after a round trip that would let a later pass run in between.
_SETTLED_THEN_SHIMMED = """() => window.__guidebot_selects.settled().then(() => ({
  shimmed: window.__guidebot_selects.isShimmed(document.getElementById('late')),
  buttons: document.querySelectorAll('[data-guidebot-select-button]').length,
}))"""


async def test_settled_waits_for_the_pass_a_fresh_select_still_owes(page: Page) -> None:
    """The bug this barrier exists for: `ready` says yes while a select is unclassified.

    `ready` resolves at the first pass and never re-arms, so a select appended
    mid-run stays bare for a whole settle window while every barrier reports
    ready — and a `select:` step landing in that window finds no DOM list to
    unfurl. `settled()` must not resolve until the owed pass has run.
    """
    await page.set_content("<body style='margin:0'></body>")
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 300};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")

    await page.evaluate(_APPEND_SELECT)
    # `ready` is the "before" reading: already settled, and wrong.
    assert not await page.evaluate(
        "() => window.__guidebot_selects.ready.then("
        " () => window.__guidebot_selects.isShimmed(document.getElementById('late')))"
    )

    assert await page.evaluate(_SETTLED_THEN_SHIMMED) == {"shimmed": True, "buttons": 1}


async def test_settled_resolves_at_once_when_no_pass_is_owed(page: Page) -> None:
    """A quiet page must cost the barrier nothing.

    Every step in compile and render takes this barrier, so charging it a settle
    window when the DOM has not moved would tax whole scenarios for the one step
    that needed it.
    """
    await page.set_content(NESTED)
    await _inject(page, {"settleMs": 2000})
    await page.wait_for_function(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('s'))",
        timeout=5000,
    )

    outcome = await page.evaluate(
        """() => Promise.race([
      window.__guidebot_selects.settled().then(() => 'settled'),
      new Promise((r) => window.setTimeout(() => r('waited'), 500)),
    ])"""
    )
    assert outcome == "settled"


async def test_settled_still_resolves_while_the_page_mutates_every_frame(page: Page) -> None:
    """The hang this barrier must never become.

    A page that mutates constantly re-arms the debounce forever — our own
    `cursor.js` does exactly that on every frame of a glide. `settled()` waits
    for the pass owed *at the moment of asking*, which the uncancellable
    deferral ceiling guarantees will run, rather than for a quiescence that
    never comes.
    """
    await page.set_content("<body style='margin:0'><div id='storm'>x</div></body>")
    await page.evaluate('window.__guidebot_selects_config = {"settleMs": 200};')
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")
    await page.evaluate(_EVERY_FRAME_STYLE_STORM)
    await page.evaluate(_APPEND_SELECT)

    outcome = await page.evaluate(
        """() => Promise.race([
      window.__guidebot_selects.settled().then(() => 'settled'),
      new Promise((r) => window.setTimeout(() => r('never settled'), 3000)),
    ])"""
    )
    assert outcome == "settled"
    assert await page.evaluate(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('late'))"
    )


async def test_a_throwing_pass_still_releases_settled(page: Page) -> None:
    """Same contract `markReady` has: a page hook that throws must not strand callers.

    The waiters are resolved from `classify`'s `finally` for this reason — a
    single hostile getter would otherwise block compile and render until their
    own timeouts.
    """
    await page.set_content("<body style='margin:0'></body>")
    await page.evaluate(
        """() => {
      window.__guidebot_selects_config = {settleMs: 50};
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
    await page.evaluate("window.__guidebot_selects.ready")
    await page.evaluate(_APPEND_SELECT)

    outcome = await page.evaluate(
        """() => Promise.race([
      window.__guidebot_selects.settled().then(() => 'settled'),
      new Promise((r) => window.setTimeout(() => r('never settled'), 2000)),
    ])"""
    )
    assert outcome == "settled"


async def test_the_observer_survives_a_document_rewrite(page: Page) -> None:
    """`document.open()` swaps `documentElement` out from under the observer.

    A MutationObserver holds the node it was given, so one bound to
    `documentElement` is left watching a detached tree the moment a page (or
    `setContent`) rewrites the document — and the shim goes deaf for good:
    nothing re-arms the debounce, so every select added afterwards stays bare
    and no barrier can wait for a pass that will never be scheduled.

    Measured before the fix: the select below was still unshimmed after 2 s with
    a 50 ms settle window, and only an explicit `refresh()` revived it.
    """
    await page.set_content("<body style='margin:0'><div id='x'>x</div></body>")
    await _inject(page, {"settleMs": 50})

    await page.evaluate(
        """() => {
      document.open();
      document.write("<body style='margin:0'></body>");
      document.close();
    }"""
    )
    await page.evaluate(_APPEND_SELECT)

    await page.wait_for_function(
        "() => window.__guidebot_selects.isShimmed(document.getElementById('late'))",
        timeout=5000,
    )
