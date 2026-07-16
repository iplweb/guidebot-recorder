"""Python controller for the full-frame text-card overlay.

Mirrors ``guidebot_recorder.overlay.overlay.Overlay`` and
``guidebot_recorder.chrome.chrome.Chrome``: a DOM overlay injected via a
Playwright init script and driven from Python through ``page.evaluate``.

Naming note: this class is called ``Slide`` to mirror ``Overlay``/``Chrome``,
but ``guidebot_recorder.models.scenario.Slide`` is the unrelated *scenario
step* model (``slide:`` in a YAML scenario). To avoid ambiguity at import
sites, ``guidebot_recorder.slide`` re-exports this class under the alias
``SlideOverlay`` — prefer ``from guidebot_recorder.slide import SlideOverlay``
there. This module keeps the bare name ``Slide`` (as opposed to e.g.
``SlideOverlay`` here too) so it mirrors ``overlay.py``'s ``Overlay`` /
``chrome.py``'s ``Chrome`` one-to-one.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from playwright.async_api import BrowserContext, Page

_API_IS_READY = """() => {
    const api = window.__guidebot_slide;
    return !!api && ["show", "hide", "ensure", "token"].every(
        (name) => typeof api[name] === "function"
    );
}"""


class Slide:
    """Install and control the full-frame text-card overlay on a Playwright page.

    Unlike the cursor/chrome overlays, no card is mounted merely by installing
    the script — the ``[data-guidebot-slide]`` node only appears once
    :meth:`show` is called with actual card content, and disappears again on
    :meth:`hide`. The card is always the TOP document only (see the ``isTop``
    guard at the top of ``slide.js``): it never mounts inside a framed site.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The appearance (dark theme) is currently hardcoded — themeable later,
        # YAGNI for now. ``config`` is accepted for interface parity with
        # ``Overlay``/``Chrome`` and forward compatibility; it is not yet
        # consulted.
        self.config = config
        body = files("guidebot_recorder.slide").joinpath("slide.js").read_text(encoding="utf-8")
        appearance = {
            "background": "#05070d",
            "titleColor": "#f8fafc",
            "subtitleColor": "#cbd5e1",
            "notesColor": "#94a3b8",
        }
        prelude = f"window.__guidebot_slide_config = {json.dumps(appearance)};\n"
        self._script = prelude + body

    async def install(self, page: Page) -> None:
        """Register the init script and evaluate it into the current document.

        Only registers the ``window.__guidebot_slide`` API — no card is shown
        until :meth:`show` is called.
        """
        await page.add_init_script(script=self._script)
        await page.evaluate(self._script)

    async def install_context(self, context: BrowserContext) -> None:
        """Register the slide API for every subsequently created/navigated document."""

        await context.add_init_script(script=self._script)

    async def _ensure_api(self, page: Page) -> None:
        """Re-inject the script if the API is missing or broken in this document."""
        if not await page.evaluate(_API_IS_READY):
            await page.evaluate(self._script)

    async def show(self, page: Page, card: dict[str, Any]) -> None:
        """Mount (replacing any existing card) with ``card = {title, subtitle, notes}``.

        Bumps the shown-token (see ``slide.js``) so callers can later
        distinguish a same-document rewrite (token survives) from a real
        navigation (fresh context, no token).
        """
        await self._ensure_api(page)
        await page.evaluate("c => window.__guidebot_slide.show(c)", card)

    async def ensure(self, page: Page, card: dict[str, Any]) -> None:
        """Idempotently repair the card if a same-document rewrite removed its DOM node.

        A no-op if the card is already present; does not bump the shown-token.
        """
        await self._ensure_api(page)
        await page.evaluate("c => window.__guidebot_slide.ensure(c)", card)

    async def hide(self, page: Page) -> None:
        """Remove the card node (if present)."""
        await self._ensure_api(page)
        await page.evaluate("() => window.__guidebot_slide.hide()")

    async def token(self, page: Page) -> Any:
        """Return the current shown-token (falsy if ``show()`` was never called
        in this JS context — i.e. a fresh document from a real navigation)."""
        await self._ensure_api(page)
        return await page.evaluate("() => window.__guidebot_slide.token()")
