"""Wspólny rozruch przeglądarki dla plików `test_validate_*.py`.

Powstał przy podziale `test_validate.py` (728 linii) na cztery pliki tematyczne.
Każdy z 42 testów tamtego pliku bierze prawdziwą stronę Playwrighta — walidacja
kompilacyjna odpowiada na pytania, na które odpowiada wyłącznie silnik
przeglądarki (drzewo dostępności, `is_visible()`, `select.options`), więc atrapa
nie sprawdziłaby tu niczego.

Świadomie NIE jest to `conftest.py` (decyzja D4 z
`docs/superpowers/specs/2026-07-22-code-cleanup-design.md`): pomocnik trzeba
zaimportować jawnie, żeby czytając plik testowy widzieć, skąd bierze się każda
nazwa.

Dzielony jest **menedżer kontekstu, nie fixture** — i to nie jest kosmetyka.
Fixture zaimportowana po nazwie (`from ._validate_page import page`) zderza się
z własną nazwą: `page` jest jednocześnie importem modułowym i argumentem każdego
testu, więc ruff zgłasza F401 na imporcie *oraz* F811 na każdym `def test_…(page)`
— a to drugie pada przy definicji testu, gdzie `# noqa` przy imporcie nie sięga
(38 wyciszeń zamiast jednego). Przy podziale na fabrykę + cienką fixture w każdym
pliku nie ma ani jednego `# noqa`, a fixture jest widoczna dokładnie tam, gdzie
jest używana — czyli tak, jak chce reguła „czytasz plik testowy i widzisz
wszystko". Współdzielona zostaje wyłącznie kosztowna połowa: start i zamknięcie
Chromium.

Uwaga przy dopisywaniu markerów: `pytestmark` nie dziedziczy się przez import
pomocnika. Każdy plik testowy nosi swoje markery sam.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Page, async_playwright


@asynccontextmanager
async def playwright_page() -> AsyncIterator[Page]:
    """Świeże, headlessowe Chromium z jedną pustą kartą, zamykane po teście."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()
