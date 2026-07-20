"""Python controller for the synthetic DOM cursor."""

from __future__ import annotations

import json
import math
from importlib.resources import files

from playwright.async_api import BrowserContext, Page

from guidebot_recorder.models.config import CursorConfig, Viewport

_API_IS_READY = """() => {
    const api = window.__guidebot_cursor;
    return !!api && ["ensure", "moveTo", "ripple", "highlight"].every(
        (name) => typeof api[name] === "function"
    );
}"""

_RESTORE_POSITION = """([x, y]) => {
    const api = window.__guidebot_cursor;
    if (!api || typeof api.ensure !== "function" || typeof api.moveTo !== "function") {
        throw new Error("guidebot cursor API is unavailable after injection");
    }
    api.ensure();
    return api.moveTo(x, y, 0);
}"""


class Overlay:
    """Install and control the synthetic cursor on a Playwright page.

    ``pos`` is authoritative across document replacements. The JavaScript init
    script creates the cursor in every new document, while :meth:`ensure`
    restores this Python-side position before the next recorded step.
    """

    def __init__(
        self, cursor: CursorConfig | None = None, viewport: Viewport | None = None
    ) -> None:
        self.cursor = cursor or CursorConfig()
        if viewport is not None:
            self.pos: tuple[float, float] = (viewport.width / 2, viewport.height / 2)
        else:
            self.pos = (0.0, 0.0)
        body = files("guidebot_recorder.overlay").joinpath("cursor.js").read_text(encoding="utf-8")
        # Prepend the appearance config as a global the injected script reads.
        # Timing (speed/min/max) stays Python-side in _glide_duration.
        appearance = {
            "width": self.cursor.width,
            "height": self.cursor.height,
            "fill": self.cursor.color,
            "stroke": self.cursor.outline,
            "glow": self.cursor.glow,
            "easing": self.cursor.easing,
            "bow": self.cursor.bow,
            "click": {
                "color": self.cursor.click.color,
                "scale": self.cursor.click.scale,
                "flash": self.cursor.click.flash,
            },
        }
        if viewport is not None:
            appearance["start"] = [self.pos[0], self.pos[1]]
        prelude = f"window.__guidebot_cursor_config = {json.dumps(appearance)};\n"
        self._script = prelude + body

    def _glide_duration(self, start: tuple[float, float], end: tuple[float, float]) -> float:
        """Duration (ms) for a move: proportional to travel distance, clamped.

        A fixed duration makes long jumps look like a snap; scaling with
        distance keeps a constant perceived hand speed.
        """
        distance = math.dist(start, end)
        return max(
            self.cursor.min_duration, min(self.cursor.max_duration, distance / self.cursor.speed)
        )

    async def install(self, page: Page) -> None:
        """Register the init script and inject it into the current document."""
        await page.add_init_script(script=self._script)
        await page.evaluate(self._script)
        await self._restore_position(page)

    async def install_context(self, context: BrowserContext) -> None:
        """Register the cursor for every subsequently created/navigated document."""

        await context.add_init_script(script=self._script)

    async def ensure(self, page: Page) -> None:
        """Recreate a missing API or DOM cursor and restore ``pos``."""
        if not await page.evaluate(_API_IS_READY):
            await page.evaluate(self._script)
        await self._restore_position(page)

    async def move_to(
        self,
        page: Page,
        x: float,
        y: float,
        ms: float | None = None,
    ) -> None:
        """Move the cursor to viewport coordinates ``(x, y)``.

        When ``ms`` is ``None`` the duration is derived from the travel
        distance (constant perceived speed); pass an explicit value to force it
        (e.g. ``ms=0`` for an instant snap on position restore).
        """
        target = (float(x), float(y))
        duration = self._glide_duration(self.pos, target) if ms is None else float(ms)
        await self.ensure(page)
        await page.evaluate(
            "([targetX, targetY, duration]) => "
            "window.__guidebot_cursor.moveTo(targetX, targetY, duration)",
            [target[0], target[1], duration],
        )
        self.pos = target

    async def ripple(self, page: Page, *, flash: bool = False) -> None:
        """Start a click ripple at the current cursor position.

        ``flash`` requests the optional filled disc under the ring; it is only
        drawn when the cursor's ``CursorClick.flash`` config also opts in.
        """
        await self.ensure(page)
        await page.evaluate("(f) => window.__guidebot_cursor.ripple(f)", flash)

    async def hide(self, page: Page) -> None:
        """Hide the cursor until :meth:`show` is called."""
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_cursor.hide()")

    async def show(self, page: Page) -> None:
        """Reveal a cursor previously hidden with :meth:`hide`."""
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_cursor.show()")

    async def _restore_position(self, page: Page) -> None:
        await page.evaluate(_RESTORE_POSITION, [self.pos[0], self.pos[1]])
