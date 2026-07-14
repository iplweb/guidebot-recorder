"""Shared debugging conveniences for compile/render."""

from __future__ import annotations

from playwright.async_api import Page


async def pause_for_inspection(
    page: Page, phase: str, index: int, kind: str, exc: Exception
) -> None:
    """Pause and leave the window open for inspection (headed). Does not mask the error."""
    print(
        f"\n⏸  {phase}: krok {index + 1} ({kind}) padł: {type(exc).__name__}: {exc}\n"
        "   Okno przeglądarki jest otwarte — obejrzyj stronę/DOM. Kliknij ▶ Resume\n"
        "   w panelu Playwright Inspector, aby kontynuować (błąd i tak zostanie zgłoszony).",
        flush=True,
    )
    try:
        await page.pause()
    except Exception:  # noqa: BLE001 — the pause is a convenience; it must not mask the step's error
        pass
