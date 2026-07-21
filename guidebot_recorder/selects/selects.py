"""Python controller for the injected ``<select>`` shim.

Mirrors :class:`guidebot_recorder.overlay.Overlay`: a JSON config prelude is
prepended to ``selects.js`` and the result is registered as a context-level init
script, so every document — including nested iframes and popup windows — gets
the widget.

Installation belongs at *every* context that drives a page through compile or
render (spec §1); routing it through :meth:`install_context` is what keeps those
call sites from drifting apart.
"""

from __future__ import annotations

import json
from importlib.resources import files

from playwright.async_api import BrowserContext

from guidebot_recorder.models.config import SelectsConfig

_AWAIT_READY = """() => {
    const api = window.__guidebot_selects;
    if (!api || !api.ready) {
        throw new Error("guidebot selects API is unavailable after injection");
    }
    return api.ready;
}"""


class Selects:
    """Install the DOM select shim on a Playwright browser context.

    The controller is stateless beyond its config: unlike the cursor, the widget
    keeps no position that must survive a document replacement — every document
    re-runs the init script and re-classifies its own selects.
    """

    def __init__(self, config: SelectsConfig | None = None) -> None:
        self.config = config or SelectsConfig()
        body = files("guidebot_recorder.selects").joinpath("selects.js").read_text(encoding="utf-8")
        # camelCase keys: the prelude is read by JavaScript, not by pydantic.
        # ``open_hold_ms`` stays Python-side — it paces the recorder's second
        # beat and the widget has no use for it.
        settings = {
            "mode": self.config.mode,
            "settleMs": self.config.settle_ms,
            "maxVisibleOptions": self.config.max_visible_options,
        }
        prelude = f"window.__guidebot_selects_config = {json.dumps(settings)};\n"
        self._script = prelude + body

    @property
    def script(self) -> str:
        """The full injected script (prelude + body), for direct evaluation."""
        return self._script

    async def install_context(self, context: BrowserContext) -> None:
        """Register the widget for every subsequently created/navigated document.

        Must be registered **before** ``chrome.js``: the widget reads the real
        ``window.top`` to decide its role, and ``chrome.js`` shadows ``top`` as
        part of frame-bust neutralization (see the contract comment in
        ``recorder/render.py``).
        """
        await context.add_init_script(script=self._script)

    async def wait_ready(self, frame) -> None:
        """Block until the frame's first classification pass has finished.

        Without this barrier a step could resolve or run against a page that has
        not been shimmed yet, so compile and render would see different DOM for
        the same instant.
        """
        await frame.evaluate(_AWAIT_READY)
