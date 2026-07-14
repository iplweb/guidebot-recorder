"""Python controller for the synthetic DOM cursor."""

from __future__ import annotations

from importlib.resources import files

from playwright.async_api import Page

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

    def __init__(self) -> None:
        self.pos: tuple[float, float] = (0.0, 0.0)
        self._script = (
            files("guidebot_recorder.overlay").joinpath("cursor.js").read_text(encoding="utf-8")
        )

    async def install(self, page: Page) -> None:
        """Register the init script and inject it into the current document."""
        await page.add_init_script(script=self._script)
        await page.evaluate(self._script)
        await self._restore_position(page)

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
        ms: float = 600,
    ) -> None:
        """Move the cursor to viewport coordinates ``(x, y)``."""
        target = (float(x), float(y))
        duration = float(ms)
        await self.ensure(page)
        await page.evaluate(
            "([targetX, targetY, duration]) => "
            "window.__guidebot_cursor.moveTo(targetX, targetY, duration)",
            [target[0], target[1], duration],
        )
        self.pos = target

    async def ripple(self, page: Page) -> None:
        """Start a click ripple at the current cursor position."""
        await self.ensure(page)
        await page.evaluate("() => window.__guidebot_cursor.ripple()")

    async def _restore_position(self, page: Page) -> None:
        await page.evaluate(_RESTORE_POSITION, [self.pos[0], self.pos[1]])
