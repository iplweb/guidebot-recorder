"""Render composed HTML to a landscape PDF via headless Chromium page.pdf()."""

from __future__ import annotations

import tempfile
from pathlib import Path

from playwright.async_api import Browser


async def html_to_pdf(browser: Browser, html: str, out_pdf: Path) -> None:
    """Write `html` to PDF. Browser MUST be headless (page.pdf throws otherwise)."""

    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "guide.html"
        index.write_text(html, encoding="utf-8")
        page = await browser.new_page()
        try:
            await page.goto(index.absolute().as_uri(), wait_until="load")
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            await page.pdf(path=str(out_pdf), landscape=True, print_background=True)
        finally:
            await page.close()
