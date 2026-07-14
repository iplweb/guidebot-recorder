"""Wspólne udogodnienia debugowe dla compile/render."""

from __future__ import annotations

from playwright.async_api import Page


async def pause_for_inspection(
    page: Page, phase: str, index: int, kind: str, exc: Exception
) -> None:
    """Zatrzymaj i zostaw okno otwarte do inspekcji (headed). Nie maskuje błędu."""
    print(
        f"\n⏸  {phase}: krok {index + 1} ({kind}) padł: {type(exc).__name__}: {exc}\n"
        "   Okno przeglądarki jest otwarte — obejrzyj stronę/DOM. Kliknij ▶ Resume\n"
        "   w panelu Playwright Inspector, aby kontynuować (błąd i tak zostanie zgłoszony).",
        flush=True,
    )
    try:
        await page.pause()
    except Exception:  # noqa: BLE001 — pauza to udogodnienie, nie może maskować błędu kroku
        pass
