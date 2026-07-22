"""Shared browser session, injector and HTML fixtures for ``test_selects_js*``.

``test_selects_js.py`` grew past the repo's 600-line-per-file limit and was
split into seven topic files: the structural invariant stayed in
``test_selects_js.py`` itself, the rest went to ``test_selects_js_shim.py``,
``_geometry``, ``_native``, ``_observer``, ``_options`` and ``_readiness``. All
seven drive the same raw script the same way, so the widget source, the browser
session, the injector and the shared markup helpers live here.

Note what is *not* here: the ``page`` fixture. Each test file declares its own
four-line ``@pytest.fixture def page`` around :func:`selects_page`. A shared
fixture re-exported by name would collide with the ``page`` parameter of every
test that uses it (ruff F811, ~70 times over), and a fixture that appears in a
file from nowhere is exactly what the no-``conftest`` convention below exists to
prevent. Only the expensive part — launching Chromium — is shared.

Deliberately **not** a ``conftest.py`` (decision D2 in
``docs/superpowers/specs/2026-07-22-code-cleanup-design.md``): a helper has to be
imported by name, so that reading a test file shows where every name in it comes
from.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files

from playwright.async_api import Page, async_playwright

from guidebot_recorder.selects.visibility import shape_prelude

# The widget body plus the one thing it is not self-contained without: the
# shared "is this select already enhanced?" predicate, which the Python
# controller prepends for exactly the same reason (``selects/visibility.py``).
SELECTS_JS = shape_prelude() + files("guidebot_recorder.selects").joinpath("selects.js").read_text(
    "utf-8"
)


@asynccontextmanager
async def selects_page() -> AsyncIterator[Page]:
    """One headless Chromium page per test, torn down with its browser."""
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page()
        try:
            yield pg
        finally:
            await b.close()


async def _inject(page: Page, cfg: dict | None = None) -> None:
    """Install the widget with a short settle window and await the first pass.

    ``settleMs: 20`` is the house convention for every test that is *not* about
    the debounce, and it is a deliberate value rather than an arbitrary one. The
    shipped default is 1000 ms (``SelectsConfig.settle_ms``), which each of the
    ~50 call sites across the seven files would otherwise pay for nothing. 20 ms
    still goes through the real debounced classification path — the pass is
    scheduled and awaited exactly as in production — while staying invisible in
    the suite's runtime.

    Tests whose subject *is* the timing pass their own value explicitly
    (``settleMs`` of 50/200/300/500/1000 all appear, mostly in
    ``test_selects_js_readiness.py`` and ``test_selects_js_observer.py``). There
    the number is the point of the test; here it is scaffolding.
    """
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


# Two animation frames: long enough for the overlay's rAF loop to have re-pinned
# and repainted. Shared by the observer and option-row files.
_TWO_FRAMES = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
