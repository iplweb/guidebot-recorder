"""Python controller for the full-frame desktop-opener overlay.

Mirrors :class:`guidebot_recorder.slide.slide.Slide`: a DOM overlay injected via
a Playwright init script and driven from Python through ``page.evaluate``. It
paints a coloured desktop with a single browser icon, exposes the icon's centre
so the caller can move the real cursor there, and animates a window growing out
of the icon — the visual half of the :class:`~guidebot_recorder.models.scenario.Desktop`
step. The other half (the cursor arc and double-click) is driven by the render
loop through the existing cursor overlay.

Re-exported as ``DesktopOverlay`` from :mod:`guidebot_recorder.desktop` to mirror
``SlideOverlay`` and avoid colliding with the scenario ``Desktop`` step model.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from importlib.resources import files
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from guidebot_recorder.models.scenario import DESKTOP_ICON_ALIASES, Desktop

_API_IS_READY = """() => {
    const api = window.__guidebot_desktop;
    return !!api && ["show", "hide", "ensure", "iconCenter", "openWindow", "token"].every(
        (name) => typeof api[name] === "function"
    );
}"""

#: Image types a scenario may point ``desktop.icon`` at. SVG is included: it is
#: rendered as an <img> data URL, not adopted into the DOM, so it cannot script.
_ICON_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _builtin_icon_svg(name: str) -> str:
    """Return the packaged SVG source for a built-in icon *name* (already aliased)."""
    asset = DESKTOP_ICON_ALIASES[name]
    return (
        files("guidebot_recorder.desktop.icons")
        .joinpath(f"{asset}.svg")
        .read_text(encoding="utf-8")
    )


def _file_icon_data_url(path: Path) -> str:
    """Encode an author-supplied icon file as a ``data:`` URL for an <img> src."""
    suffix = path.suffix.lower()
    if suffix not in _ICON_SUFFIXES:
        raise ValueError(
            f"nieobsługiwany format ikony pulpitu {path.name!r}; "
            f"dozwolone: {', '.join(sorted(_ICON_SUFFIXES))}"
        )
    mime = mimetypes.types_map.get(suffix, "image/svg+xml" if suffix == ".svg" else None)
    if mime is None:  # pragma: no cover - defensive; every suffix above maps
        raise ValueError(f"nie rozpoznano typu MIME ikony {path.name!r}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def resolve_icon(desktop: Desktop, *, base_dir: Path | None = None) -> dict[str, str]:
    """Turn a step's ``icon`` into the payload the JS overlay expects.

    A built-in name yields ``{"iconSvg": <packaged svg>}``; a path yields
    ``{"iconImg": <data url>}``. Relative paths resolve against *base_dir* (the
    scenario file's directory) so a scenario can ship its own icon beside it.

    Fail-loud on a bad path or unknown built-in: a desktop step naming an icon
    that cannot be drawn is an authoring error, not something to paper over with
    a blank square.
    """
    icon = desktop.icon
    if desktop.is_builtin_icon():
        return {"iconSvg": _builtin_icon_svg(icon)}
    path = Path(icon)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if not path.is_file():
        raise ValueError(
            f"ikona pulpitu {icon!r} nie jest ani wbudowaną nazwą "
            f"({', '.join(sorted(set(DESKTOP_ICON_ALIASES)))}) ani istniejącym plikiem"
        )
    return {"iconImg": _file_icon_data_url(path)}


class DesktopOverlay:
    """Install and control the desktop-opener overlay on a Playwright page.

    Named ``DesktopOverlay`` rather than ``Desktop`` (as ``Slide`` is) because
    this module also imports the scenario ``Desktop`` step model for
    :func:`resolve_icon`; the two must not collide.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        body = files("guidebot_recorder.desktop").joinpath("desktop.js").read_text(encoding="utf-8")
        appearance = {
            "background": self.config.get("background", "#1f3a63"),
            "labelColor": "#f8fafc",
            "windowColor": "#ffffff",
        }
        prelude = f"window.__guidebot_desktop_config = {json.dumps(appearance)};\n"
        self._script = prelude + body

    async def install(self, page: Page) -> None:
        await page.add_init_script(script=self._script)
        await page.evaluate(self._script)

    async def install_context(self, context: BrowserContext) -> None:
        await context.add_init_script(script=self._script)

    async def _ensure_api(self, page: Page) -> None:
        if not await page.evaluate(_API_IS_READY):
            await page.evaluate(self._script)

    async def show(self, page: Page, desktop: dict[str, Any]) -> None:
        """Paint the desktop with ``desktop = {color, label, iconSvg|iconImg}``."""
        await self._ensure_api(page)
        await page.evaluate("d => window.__guidebot_desktop.show(d)", desktop)

    async def ensure(self, page: Page, desktop: dict[str, Any]) -> None:
        await self._ensure_api(page)
        await page.evaluate("d => window.__guidebot_desktop.ensure(d)", desktop)

    async def icon_center(self, page: Page) -> tuple[float, float] | None:
        """Viewport centre of the icon glyph, or ``None`` if no desktop is up."""
        await self._ensure_api(page)
        point = await page.evaluate("() => window.__guidebot_desktop.iconCenter()")
        if not isinstance(point, dict):
            return None
        return float(point["x"]), float(point["y"])

    async def open_window(self, page: Page, ms: float) -> bool:
        """Animate the window growing from the icon over *ms* milliseconds."""
        await self._ensure_api(page)
        return bool(await page.evaluate("m => window.__guidebot_desktop.openWindow(m)", float(ms)))

    async def hide(self, page: Page) -> None:
        await self._ensure_api(page)
        await page.evaluate("() => window.__guidebot_desktop.hide()")

    async def token(self, page: Page) -> Any:
        await self._ensure_api(page)
        return await page.evaluate("() => window.__guidebot_desktop.token()")
