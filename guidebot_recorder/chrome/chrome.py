"""Python controller for the optional browser chrome DOM overlay."""

from __future__ import annotations

import json
from importlib.resources import files

from playwright.async_api import BrowserContext, Page

from guidebot_recorder.models.config import ChromeConfig

_API_IS_READY = """() => {
    const api = window.__guidebot_chrome;
    return !!api && ["ensure", "setUrl"].every(
        (name) => typeof api[name] === "function"
    );
}"""


class Chrome:
    """Install and control the macOS-style browser bar used during render."""

    def __init__(self, config: ChromeConfig | None = None) -> None:
        self.config = config or ChromeConfig()
        body = files("guidebot_recorder.chrome").joinpath("chrome.js").read_text(encoding="utf-8")
        appearance = {
            "showUrl": self.config.show_url,
            "height": self.config.height,
            "barColor": self.config.bar_color,
            "textColor": self.config.text_color,
            "radius": self.config.radius,
            "showLock": self.config.show_lock,
            "closeColor": self.config.close_color,
            "minimizeColor": self.config.minimize_color,
            "maximizeColor": self.config.maximize_color,
        }
        prelude = f"window.__guidebot_chrome_config = {json.dumps(appearance)};\n"
        self._script = prelude + body

    async def install(self, page: Page) -> None:
        """Register the init script and inject the bar into the current document."""
        await page.add_init_script(script=self._script)
        await page.evaluate(self._script)
        await self.ensure(page)

    async def install_context(self, context: BrowserContext) -> None:
        """Register the bar before pages are created so their first frame has it."""

        await context.add_init_script(script=self._script)

    async def ensure(self, page: Page) -> None:
        """Recreate a missing controller/bar and synchronize Playwright's URL."""
        if not await page.evaluate(_API_IS_READY):
            await page.evaluate(self._script)
        await page.evaluate("url => window.__guidebot_chrome.ensure(url)", page.url)

    async def set_url(self, page: Page, url: str, *, animate: bool = True) -> None:
        """Show ``url`` instantly or type it character by character.

        Playwright awaits promises returned by ``page.evaluate``, so this method
        only returns after the JavaScript typing animation has finished.
        """
        await self.ensure(page)
        await page.evaluate(
            "([targetUrl, shouldAnimate]) => "
            "window.__guidebot_chrome.setUrl(targetUrl, shouldAnimate)",
            [url, animate],
        )
