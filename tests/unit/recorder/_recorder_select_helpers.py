"""Shared scaffolding for the ``test_recorder_select_*`` family.

``test_recorder_select.py`` grew past the repo's 600-line-per-file limit and was
split by area: the two-beats choreography (``test_recorder_select_beats.py``),
the ``native`` escape hatch (``_native``), verification / error diagnosis
(``_verify``), the natively-visible listbox path (``_listbox``), the compile-path
drivability probe (``_probe``), the readiness barrier (``_readiness``), the
still-capture ``on_revealed`` hook (``_reveal``) and which refusals mean the
option is not on offer (``_reason``). Every one of them drives a real Chromium
page with ``selects.js`` evaluated directly and a short settle window, so the
widget is classified before the recorder drives it — patterned on
``tests/unit/selects/test_selects_js.py``.

Note what is *not* here: the ``page`` fixture. Each test file declares its own
four-line ``@pytest.fixture def page`` around :func:`selects_page`. A shared
fixture re-exported by name would collide with the ``page`` parameter of every
test that takes it (ruff F811, ~37 times over), and a fixture that appears in a
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

from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.selects.visibility import shape_prelude

# Body plus the shared "already enhanced?" predicate the Python controller
# prepends in production (``selects/visibility.py``).
SELECTS_JS = shape_prelude() + files("guidebot_recorder.selects").joinpath("selects.js").read_text(
    "utf-8"
)


@asynccontextmanager
async def selects_page() -> AsyncIterator[Page]:
    """One headless Chromium page per test, torn down with its browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        pg = await browser.new_page()
        try:
            yield pg
        finally:
            await browser.close()


async def _install_selects(page: Page, **cfg: object) -> None:
    """Install the widget with a short settle window and await the first pass."""

    merged = {"settleMs": 20, **cfg}
    await page.evaluate(f"window.__guidebot_selects_config = {json.dumps(merged)};")
    await page.evaluate(SELECTS_JS)
    await page.evaluate("window.__guidebot_selects.ready")


_MOUSEDOWN_SPY = """() => {
  window.__gbHits = [];
  document.addEventListener("mousedown", (event) => {
    const el = event.target;
    if (!el || !el.getAttribute) return;
    if (el.hasAttribute("data-guidebot-option-index")) {
      window.__gbHits.push("option:" + el.getAttribute("data-guidebot-option-index"));
    } else if (el.hasAttribute("data-guidebot-select-button")) {
      window.__gbHits.push("button");
    } else {
      window.__gbHits.push(el.tagName.toLowerCase());
    }
  }, true);
}"""


async def _hits(page: Page) -> list[str]:
    return await page.evaluate("() => window.__gbHits")


_RAW_SELECT = (
    "<body style='margin:0'>"
    "<select aria-label='Raport' style='width:220px'>"
    "<option>lista</option><option>tabela</option><option>BibTeX</option>"
    "</select></body>"
)


async def _raw_page(page: Page) -> Overlay:
    overlay = Overlay()
    await page.set_content(_RAW_SELECT)
    await overlay.install(page)
    await _install_selects(page)
    await page.evaluate(_MOUSEDOWN_SPY)
    return overlay


def _enhanced(labels: list[str], rows: list[str]) -> str:
    """A hidden ``<select>`` plus a sibling widget that opens a body-level list.

    Reproduces the select2 / Tom Select *pattern* rather than vendoring either.
    """

    return (
        "<body style='margin:0'>"
        "<select id='s' data-testid='s' style='display:none'>"
        + "".join(f"<option>{label}</option>" for label in labels)
        + "</select>"
        "<div data-testid='w' id='w' style='width:200px;height:30px;border:1px solid #000'>"
        f"{labels[0]}</div>"
        "<script>"
        "document.getElementById('w').addEventListener('click', () => {"
        "  const list = document.createElement('div');"
        "  list.id = 'fake-list';"
        "  list.style.cssText = 'position:fixed;top:120px;left:0;width:200px;background:#fff';"
        f"  for (const label of {json.dumps(rows)}) {{"
        "    const row = document.createElement('div');"
        "    row.textContent = label;"
        "    row.style.cssText = 'padding:4px';"
        "    row.addEventListener('click', () => {"
        "      const sel = document.getElementById('s');"
        "      sel.value = label;"
        "      sel.dispatchEvent(new Event('change', {bubbles: true}));"
        "      list.remove();"
        "    });"
        "    list.appendChild(row);"
        "  }"
        "  document.body.appendChild(list);"
        "});"
        "</script></body>"
    )


_DISABLED_OPTION_SELECT = (
    "<body style='margin:0'>"
    "<select aria-label='Raport' style='width:220px'>"
    "<option>lista</option><option disabled>tabela</option><option>BibTeX</option>"
    "</select></body>"
)


def _enhanced_with_decoy(labels: list[str], rows: list[str], decoy: str) -> str:
    """The page-widget pattern plus a live region that echoes the option label.

    The decoy is prepended to ``<body>``, so it precedes the widget's own list
    in document order — which is exactly the tie-break the "appeared after the
    click" heuristic applies. A toast, an aria-live region or a "current
    selection" readout is an everyday piece of a dropdown widget.
    """

    return (
        "<body style='margin:0'>"
        "<select id='s' data-testid='s' style='display:none'>"
        + "".join(f"<option>{label}</option>" for label in labels)
        + "</select>"
        "<div data-testid='w' id='w' style='width:200px;height:30px;border:1px solid #000'>"
        f"{labels[0]}</div>"
        "<script>"
        "document.getElementById('w').addEventListener('click', () => {"
        "  const toast = document.createElement('div');"
        "  toast.id = 'toast';"
        f"  toast.textContent = {json.dumps(decoy)};"
        "  toast.style.cssText = 'width:200px;height:20px';"
        "  document.body.prepend(toast);"
        "  const list = document.createElement('div');"
        "  list.id = 'fake-list';"
        "  list.style.cssText = 'position:fixed;top:120px;left:0;width:200px;background:#fff';"
        f"  for (const label of {json.dumps(rows)}) {{"
        "    const row = document.createElement('div');"
        "    row.textContent = label;"
        "    row.style.cssText = 'padding:4px';"
        "    row.addEventListener('click', () => {"
        "      const sel = document.getElementById('s');"
        "      sel.value = label;"
        "      sel.dispatchEvent(new Event('change', {bubbles: true}));"
        "      list.remove();"
        "    });"
        "    list.appendChild(row);"
        "  }"
        "  document.body.appendChild(list);"
        "});"
        "</script></body>"
    )


def _listbox(attrs: str, labels: list[str], selected: str | None = None) -> str:
    options = "".join(
        f"<option{' selected' if label == selected else ''}>{label}</option>" for label in labels
    )
    return (
        f"<body style='margin:0'><select id='s' data-testid='s' aria-label='Tagi' "
        f"{attrs} style='width:220px'>{options}</select></body>"
    )


_SELECTED_JS = "() => [...document.querySelector('select').selectedOptions].map((o) => o.label)"


async def _listbox_page(page: Page, attrs: str, labels: list[str], selected=None) -> Overlay:
    overlay = Overlay()
    await page.set_content(_listbox(attrs, labels, selected))
    await overlay.install(page)
    await _install_selects(page)
    await page.evaluate(_MOUSEDOWN_SPY)
    return overlay
