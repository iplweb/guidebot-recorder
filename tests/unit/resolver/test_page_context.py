from collections.abc import AsyncIterator
from dataclasses import fields, is_dataclass

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.resolver.page_context import Candidate, collect_candidates


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 640, "height": 480})
        try:
            yield page
        finally:
            await browser.close()


def test_candidate_is_the_page_context_data_contract() -> None:
    assert is_dataclass(Candidate)
    assert [field.name for field in fields(Candidate)] == [
        "id",
        "role",
        "name",
        "tag",
        "bbox",
        "visible",
        "enabled",
        "ancestry",
    ]

    candidate = Candidate(
        id="candidate-1",
        role="button",
        name="Zaloguj",
        tag="button",
        bbox=(10.0, 20.0, 80.0, 30.0),
        visible=True,
        enabled=True,
        ancestry=[("main", "main")],
    )

    assert candidate.bbox == (10.0, 20.0, 80.0, 30.0)
    assert candidate.ancestry == [("main", "main")]


async def test_collects_visible_interactive_elements_and_headings(page: Page) -> None:
    await page.set_content(
        """
        <main>
          <section aria-label="Konto">
            <h2>Panel klienta</h2>
            <p>Opis, który nie jest kandydatem.</p>
            <button>Zaloguj</button>
            <a href="/help">Pomoc</a>
            <button style="display: none">Ukryty</button>
          </section>
        </main>
        """
    )

    candidates = await collect_candidates(page)
    by_name = {candidate.name: candidate for candidate in candidates}

    login = by_name["Zaloguj"]
    assert login.role == "button"
    assert login.tag == "button"
    assert login.visible is True
    assert login.enabled is True
    assert login.id
    assert len(login.bbox) == 4
    assert all(isinstance(value, float) for value in login.bbox)
    assert login.bbox[2] > 0
    assert login.bbox[3] > 0
    assert isinstance(login.ancestry, list)
    assert all(
        isinstance(item, tuple)
        and len(item) == 2
        and all(isinstance(value, str) for value in item)
        for item in login.ancestry
    )

    assert by_name["Panel klienta"].role == "heading"
    assert by_name["Pomoc"].role == "link"
    assert "Opis, który nie jest kandydatem." not in by_name
    assert "Ukryty" not in by_name


async def test_reports_enabled_state_for_disabled_candidates(page: Page) -> None:
    await page.set_content(
        """
        <button disabled>Usuń konto</button>
        <input aria-label="Adres e-mail">
        """
    )

    candidates = await collect_candidates(page)
    by_name = {candidate.name: candidate for candidate in candidates}

    assert by_name["Usuń konto"].enabled is False
    assert by_name["Adres e-mail"].role == "textbox"
    assert by_name["Adres e-mail"].enabled is True


async def test_viewport_only_excludes_visible_elements_below_the_fold(
    page: Page,
) -> None:
    await page.set_content(
        """
        <style>
          body { margin: 0; }
          #below-fold { position: absolute; top: 900px; }
        </style>
        <button>W kadrze</button>
        <button id="below-fold">Poza kadrem</button>
        """
    )

    viewport_candidates = await collect_candidates(page)
    all_candidates = await collect_candidates(page, viewport_only=False)

    assert {candidate.name for candidate in viewport_candidates} == {"W kadrze"}
    assert {candidate.name for candidate in all_candidates} == {
        "W kadrze",
        "Poza kadrem",
    }
    assert next(
        candidate for candidate in all_candidates if candidate.name == "Poza kadrem"
    ).visible is True


async def test_limit_is_hard_and_candidate_ids_are_stable(page: Page) -> None:
    await page.set_content(
        """
        <button>Pierwszy</button>
        <button>Drugi</button>
        <button>Trzeci</button>
        <button>Czwarty</button>
        """
    )

    first_collection = await collect_candidates(page, limit=2)
    second_collection = await collect_candidates(page, limit=2)

    assert len(first_collection) == 2
    assert len(second_collection) == 2
    assert all(candidate.id for candidate in first_collection)
    assert len({candidate.id for candidate in first_collection}) == 2
    assert [candidate.id for candidate in first_collection] == [
        candidate.id for candidate in second_collection
    ]
