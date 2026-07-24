"""Shared scaffolding for the split ``test_compile_*.py`` files.

This repo deliberately uses no ``conftest.py``; shared test scaffolding is
imported explicitly from here instead. ``test_compile.py`` was split by area
into ``test_compile_cache.py``, ``test_compile_reuse.py``,
``test_compile_typing.py``, ``test_compile_popup.py``,
``test_compile_closewindow.py`` and ``test_compile_describe.py``; the pieces
those files share live here.

The browser ``page`` fixture is exported as a **factory** (:func:`make_page`),
not as a fixture. A fixture named ``page`` re-imported into a file whose tests
take ``page`` as a parameter is a redefinition that ruff flags as F811 — which
``# noqa: F401`` does not silence, and which pinned ruff 0.9.2 reports
regardless of any alias trick. So each test file declares its own one-line
``page`` fixture that drives this async generator::

    @pytest.fixture
    async def page():
        async for pg in make_page():
            yield pg

``SCENARIO`` and ``MockReasoner`` are plain values used across several of the
split files, so they are imported by name (no fixture, no F811 risk).
"""

import textwrap

from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.resolver.reasoner import ReasonerResult

SCENARIO = textwrap.dedent(
    """\
    config:
      title: Logowanie
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - teach: "kliknij Zaloguj"
    """
)


class MockReasoner:
    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


async def make_page():
    """Async generator backing each file's thin ``page`` fixture."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()
