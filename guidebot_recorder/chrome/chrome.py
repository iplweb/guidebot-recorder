"""Python controller for the optional browser chrome DOM overlay."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.resources import files

from playwright.async_api import BrowserContext, Frame, Page

from guidebot_recorder.chrome.typing import typing_schedule
from guidebot_recorder.models.config import ChromeConfig

#: Sentinel origin/URL the main render window is navigated to so injected
#: scripts can detect the shell role deterministically (kept in sync with the
#: ``SHELL_ORIGIN`` literals in ``chrome.js`` and ``shell.js``).
SHELL_ORIGIN = "https://guidebot.shell"
SHELL_URL = SHELL_ORIGIN + "/"
_SHELL_ROUTE_GLOB = SHELL_ORIGIN + "/**"
_SITE_IFRAME_SELECTOR = "iframe#guidebot-site"

_API_IS_READY = """() => {
    const api = window.__guidebot_chrome;
    return !!api && ["ensure", "setUrl"].every(
        (name) => typeof api[name] === "function"
    );
}"""

_SHELL_IS_READY = """() => {
    const api = window.__guidebot_shell;
    const ready = !!api && [
        "pillRect", "focusPill", "blurPill", "clearUrl", "appendChar", "setUrl"
    ].every((name) => typeof api[name] === "function");
    return ready
        && !!document.getElementById("guidebot-site")
        && !!document.querySelector("[data-guidebot-shell-bar]");
}"""

#: Determines which chrome surface (if any) currently owns the page, so
#: ``hide``/``show`` can dispatch without relying on URL matching.
_ROLE_PROBE = """() => {
    if (window.__guidebot_shell) {
        return "shell";
    }
    if (window.__guidebot_chrome) {
        return "chrome";
    }
    return "none";
}"""


class Chrome:
    """Install and control the macOS-style browser bar used during render.

    Two independent surfaces share one controller:

    - the **legacy** in-DOM padding bar (``ensure``/``set_url``) still used for
      popup documents (``popup-site`` role);
    - the **shell** (``install_shell``/``type_url``/``set_url_shell``) that owns
      the main render window — a bar plus a sandboxed site iframe.
    """

    def __init__(self, config: ChromeConfig | None = None, *, bare_popups: bool = False) -> None:
        self.config = config or ChromeConfig()
        self.bare_popups = bare_popups
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
        # ``barePopups`` suppresses the legacy in-DOM padding bar on popup-site
        # documents: floating popups are framed by the post-process compositor,
        # not the page's own chrome (see PopupConfig.floating). It is a behavior
        # flag, so it rides on the chrome prelude only, not the shell appearance.
        prelude_config = {**appearance, "barePopups": bare_popups}
        prelude = f"window.__guidebot_chrome_config = {json.dumps(prelude_config)};\n"
        self._script = prelude + body

        shell_body = files("guidebot_recorder.chrome").joinpath("shell.js").read_text(
            encoding="utf-8"
        )
        shell_appearance = {**appearance, "focusColor": self.config.focus_color,
                            "showCaret": self.config.show_caret}
        self._shell_script = shell_body
        self._shell_html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>guidebot</title>"
            "<style>html,body{margin:0;padding:0;height:100%;"
            "background:#fff;overflow:hidden}</style></head><body>"
            f"<script>window.__guidebot_shell_config = {json.dumps(shell_appearance)};</script>"
            f"<script>{shell_body}</script></body></html>"
        )

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
        """Show ``url`` instantly or type it character by character (legacy bar).

        Playwright awaits promises returned by ``page.evaluate``, so this method
        only returns after the JavaScript typing animation has finished.
        """
        await self.ensure(page)
        await page.evaluate(
            "([targetUrl, shouldAnimate]) => "
            "window.__guidebot_chrome.setUrl(targetUrl, shouldAnimate)",
            [url, animate],
        )

    async def hide(self, page: Page) -> None:
        """Hide whichever bar currently owns ``page`` (shell or legacy chrome).

        Dispatches by probing which API is present rather than URL matching, so
        it works regardless of role. A page with neither API installed is a
        no-op — there is nothing to hide.
        """
        role = await page.evaluate(_ROLE_PROBE)
        if role == "shell":
            await self.ensure_shell(page)
            await page.evaluate("() => window.__guidebot_shell.hide()")
        elif role == "chrome":
            await self.ensure(page)
            await page.evaluate("() => window.__guidebot_chrome.hide()")

    async def show(self, page: Page) -> None:
        """Show whichever bar currently owns ``page`` (shell or legacy chrome).

        Mirrors :meth:`hide`'s role dispatch; a no-op when neither API is
        installed.
        """
        role = await page.evaluate(_ROLE_PROBE)
        if role == "shell":
            await self.ensure_shell(page)
            await page.evaluate("() => window.__guidebot_shell.show()")
        elif role == "chrome":
            await self.ensure(page)
            await page.evaluate("() => window.__guidebot_chrome.show()")

    # --- Shell (main render window) -----------------------------------------

    async def install_shell(self, page: Page) -> Frame:
        """Serve the shell from the sentinel origin, load it, and return the site frame.

        Registers a route (after ``install_framing`` so it takes precedence for
        the sentinel URL), navigates the main page to the shell, then hands back
        the ``Frame`` of the ``<iframe>`` the target site will be driven in.
        """

        await page.context.route(_SHELL_ROUTE_GLOB, self._serve_shell)
        await page.goto(SHELL_URL, wait_until="load")
        await self.ensure_shell(page)
        return await self.site_frame(page)

    async def _serve_shell(self, route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=self._shell_html,
        )

    async def ensure_shell(self, page: Page) -> None:
        """Assert (and repair) the shell invariant: bar + iframe present in the shell doc."""

        if not await page.evaluate(_SHELL_IS_READY):
            await page.evaluate(self._shell_script)
        if not await page.evaluate(_SHELL_IS_READY):
            raise RuntimeError("powłoka chrome (pasek/iframe) jest niedostępna po wstrzyknięciu")

    async def site_frame(self, page: Page) -> Frame:
        """Return the ``Frame`` of the shell's site iframe."""

        handle = await page.wait_for_selector(_SITE_IFRAME_SELECTOR, state="attached")
        frame = await handle.content_frame()
        if frame is None:
            raise RuntimeError("nie udało się uzyskać ramki witryny w powłoce chrome")
        return frame

    async def set_url_shell(self, page: Page, url: str) -> None:
        """Set the shell pill to ``url`` instantly (reflects the final/redirected URL)."""

        await self.ensure_shell(page)
        await page.evaluate("u => window.__guidebot_shell.setUrl(u)", url)

    async def type_url(
        self,
        page: Page,
        overlay,
        url: str,
        *,
        seed: str,
        choreograph: bool,
        on_sfx: Callable[[str], None] | None = None,
    ) -> None:
        """Drive the address-bar choreography in the shell before navigation.

        ``choreograph`` runs the full pointer sequence (glide to the pill, ripple,
        focus); otherwise only the natural typing animation plays. In both cases
        the URL is typed character by character, paced by :func:`typing_schedule`
        so the per-character delay sequence is deterministic across re-renders.
        ``on_sfx`` (when given) is called ``"click"`` at the pill click and
        ``"key"`` per typed character, so the address bar is audible like the page.
        """

        await self.ensure_shell(page)
        if choreograph:
            rect = await page.evaluate("() => window.__guidebot_shell.pillRect()")
            cx = rect["x"] + rect["width"] / 2
            cy = rect["y"] + rect["height"] / 2
            await overlay.move_to(page, cx, cy)
            await overlay.ripple(page)
            if on_sfx is not None:
                on_sfx("click")
        await page.evaluate("() => window.__guidebot_shell.focusPill()")
        await page.evaluate("() => window.__guidebot_shell.clearUrl()")
        delays = typing_schedule(
            url,
            char_delay_ms=self.config.char_delay_ms,
            char_jitter_ms=self.config.char_jitter_ms,
            segment_pause_ms=self.config.segment_pause_ms,
            seed=seed,
        )
        for character, delay in zip(url, delays, strict=True):
            await page.wait_for_timeout(delay)
            await page.evaluate("c => window.__guidebot_shell.appendChar(c)", character)
            if on_sfx is not None:
                on_sfx("key")
        await page.wait_for_timeout(self.config.pre_navigate_pause_ms)
        await page.evaluate("() => window.__guidebot_shell.blurPill()")
