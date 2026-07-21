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

import asyncio
import json
from importlib.resources import files

from playwright.async_api import BrowserContext

from guidebot_recorder.models.config import SelectsConfig

# Default ceiling for :meth:`Selects.wait_ready`. Generous next to any sane
# ``settle_ms`` (the widget's own uncancellable first pass fires one settle
# window after DOMContentLoaded), so it only ever fires when something is truly
# wedged.
READY_TIMEOUT = 15.0

_AWAIT_READY = """(timeoutMs) => {
    const api = window.__guidebot_selects;
    if (!api || !api.ready) {
        throw new Error("guidebot selects API is unavailable after injection");
    }
    // Race in the page as well as in Python: an unresolved `ready` must surface
    // as an error, never as an evaluate that never returns.
    return Promise.race([
        api.ready,
        new Promise((_resolve, reject) => {
            window.setTimeout(() => reject(new Error("guidebot selects ready timeout")), timeoutMs);
        }),
    ]);
}"""


class SelectsNotReadyError(RuntimeError):
    """The widget never reported readiness for a frame within the timeout."""


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

        Registration order relative to ``chrome.js`` does not change this
        widget's role gating, unlike ``cursor.js``/``slide.js``: the shim bails
        only on ``isTop and origin == SHELL_ORIGIN``, and ``chrome.js`` shadows
        ``window.top`` only inside framed documents, whose origin is never the
        shell's. See the role-gating comment at the top of ``selects.js``.
        """
        await context.add_init_script(script=self._script)

    async def wait_ready(self, frame, timeout: float = READY_TIMEOUT) -> None:
        """Block until the frame's first classification pass has finished.

        Without this barrier a step could resolve or run against a page that has
        not been shimmed yet, so compile and render would see different DOM for
        the same instant.

        Bounded on purpose: waiting forever would turn any page whose widget
        never settles into a compile or render that hangs with no diagnosis.

        Raises:
            SelectsNotReadyError: the widget did not settle within ``timeout``.
        """
        timeout_ms = max(1, int(timeout * 1000))
        try:
            # The page-side race is the primary guard; the outer wait covers the
            # case where the document itself stops running timers.
            await asyncio.wait_for(
                frame.evaluate(_AWAIT_READY, timeout_ms),
                timeout=timeout + 1.0,
            )
        except TimeoutError as exc:
            raise SelectsNotReadyError(
                f"widget select nie zgłosił gotowości w ciągu {timeout:.1f} s"
            ) from exc
        except Exception as exc:
            if "guidebot selects ready timeout" not in str(exc):
                raise
            raise SelectsNotReadyError(
                f"widget select nie zgłosił gotowości w ciągu {timeout:.1f} s"
            ) from exc
