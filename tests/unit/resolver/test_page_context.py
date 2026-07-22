from collections.abc import AsyncIterator
from dataclasses import fields, is_dataclass
from hashlib import sha256

import pytest
from playwright.async_api import Page, async_playwright

from guidebot_recorder.resolver.page_context import (
    Candidate,
    candidate_id_for_path,
    candidate_ids_of,
    collect_candidates,
)


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
        isinstance(item, tuple) and len(item) == 2 and all(isinstance(value, str) for value in item)
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


async def test_explicit_checkbox_uses_its_content_as_accessible_name(
    page: Page,
) -> None:
    await page.set_content('<div role="checkbox">Zapamiętaj mnie</div>')

    candidates = await collect_candidates(page)

    assert any(
        candidate.role == "checkbox" and candidate.name == "Zapamiętaj mnie"
        for candidate in candidates
    )


async def test_advertised_password_role_is_resolvable_by_playwright(
    page: Page,
) -> None:
    await page.set_content(
        """
        <label for="password">Hasło</label>
        <input id="password" type="password">
        """
    )

    candidates = await collect_candidates(page)
    password = next(candidate for candidate in candidates if candidate.tag == "input")

    assert await page.get_by_role(password.role, name=password.name, exact=True).count() == 1


async def test_css_hidden_descendant_is_excluded_from_accessible_name(
    page: Page,
) -> None:
    await page.set_content(
        """
        <button>Zapisz <span style="display: none">tajny sufiks</span></button>
        """
    )

    candidates = await collect_candidates(page)

    button = next(candidate for candidate in candidates if candidate.role == "button")
    assert button.name == "Zapisz"


async def test_hidden_aria_labelledby_reference_still_supplies_name(
    page: Page,
) -> None:
    await page.set_content(
        """
        <span id="publish-label" hidden>Opublikuj raport</span>
        <button aria-labelledby="publish-label"></button>
        """
    )

    candidates = await collect_candidates(page)

    assert any(
        candidate.role == "button" and candidate.name == "Opublikuj raport"
        for candidate in candidates
    )


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
    assert (
        next(candidate for candidate in all_candidates if candidate.name == "Poza kadrem").visible
        is True
    )


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


async def test_candidate_ids_are_unique_across_a_shadow_root(page: Page) -> None:
    """Siblings directly under a shadow root have no ``parentElement``.

    Without an index on every segment their DOM paths collapse into one, and so
    do their ids — which, now that the id carries the intent, would silently
    freeze the wrong element.
    """

    await page.set_content(
        """
        <div id="host"></div>
        <script>
          const root = document.getElementById("host").attachShadow({ mode: "open" });
          root.innerHTML = "<button>Pierwszy</button><button>Drugi</button>";
        </script>
        """
    )

    candidates = await collect_candidates(page)
    by_name = {candidate.name: candidate for candidate in candidates}

    assert {"Pierwszy", "Drugi"} <= set(by_name)
    assert by_name["Pierwszy"].id != by_name["Drugi"].id
    assert len({candidate.id for candidate in candidates}) == len(candidates)


async def test_candidate_ids_keep_exact_nth_of_type_path_semantics(page: Page) -> None:
    await page.set_content(
        """
        <button>Pierwszy</button>
        <span>Rozdzielacz</span>
        <button>Drugi</button>
        """
    )

    candidates = await collect_candidates(page)
    by_name = {candidate.name: candidate for candidate in candidates}

    def candidate_id(path: str) -> str:
        return "candidate-" + sha256(path.encode("utf-8")).hexdigest()[:16]

    first_path = "html:nth-of-type(1)>body:nth-of-type(1)>button:nth-of-type(1)"
    second_path = "html:nth-of-type(1)>body:nth-of-type(1)>button:nth-of-type(2)"

    assert by_name["Pierwszy"].id == candidate_id(first_path)
    assert by_name["Drugi"].id == candidate_id(second_path)

    # The public helper is the only definition of the id; it must agree with the
    # formula pinned above rather than restate it.
    assert candidate_id_for_path(first_path) == candidate_id(first_path)


async def test_candidate_ids_of_follow_the_order_locator_nth_indexes(page: Page) -> None:
    await page.set_content(
        """
        <button>Pierwszy</button>
        <span>Rozdzielacz</span>
        <button>Drugi</button>
        <button>Trzeci</button>
        """
    )

    locator = page.get_by_role("button")
    ids = await candidate_ids_of(locator)

    assert len(ids) == await locator.count() == 3
    assert len(set(ids)) == 3
    for index, expected in enumerate(ids):
        assert await candidate_ids_of(locator.nth(index)) == [expected]

    collected = {candidate.name: candidate.id for candidate in await collect_candidates(page)}
    assert ids == [collected["Pierwszy"], collected["Drugi"], collected["Trzeci"]]


async def test_candidate_ids_of_sees_elements_inside_shadow_roots(page: Page) -> None:
    await page.set_content(
        """
        <div id="host"></div>
        <script>
          const root = document.getElementById("host").attachShadow({ mode: "open" });
          root.innerHTML = "<button>Pierwszy</button><button>Drugi</button>";
        </script>
        """
    )

    locator = page.get_by_role("button")
    ids = await candidate_ids_of(locator)

    assert len(ids) == 2
    assert len(set(ids)) == 2


async def test_candidate_ids_stay_unique_for_siblings_differing_only_in_case(
    page: Page,
) -> None:
    """`nth-of-type` liczyło rodzeństwo z rozróżnianiem wielkości liter, a ścieżka nie.

    Segment ścieżki emituje `localName.toLowerCase()`, więc `<myTag>` i `<mytag>`
    dawały ten sam tekst — ale licznik pomijał się nawzajem jako „inny tag"
    i obu przypisywał `nth-of-type(1)`. Wynik: jedno `candidateId` dla dwóch
    różnych elementów, czyli dokładnie ta nieunikalność, na której stoi
    przypinanie. Parser HTML normalizuje nazwy, więc kolizję da się zbudować
    tylko przez `createElementNS` (tak jak robią to biblioteki SVG/MathML).
    """

    await page.set_content("<div id='host'></div>")
    await page.evaluate(
        """() => {
          const host = document.getElementById('host');
          const ns = 'http://www.w3.org/2000/svg';
          for (const name of ['myTag', 'mytag']) {
            const element = document.createElementNS(ns, name);
            element.textContent = name;
            host.appendChild(element);
          }
        }"""
    )

    ids = await candidate_ids_of(page.locator("#host > *"))

    assert len(ids) == 2
    assert len(set(ids)) == 2


async def test_candidate_ids_of_index_a_chained_scoped_locator_exactly_like_nth(
    page: Page,
) -> None:
    """Namiar ze `scope` to lokator łańcuchowy — a to on jest indeksowany przez `nth`.

    Docstring `candidate_ids_of` stawia tu jawną tezę: Playwright składa człony
    „po kolei dla każdego korzenia zawężenia", niekoniecznie w kolejności
    dokumentu. Teza jest nieszkodliwa dokładnie dlatego, że obie strony czytają
    tę samą listę — i to jest jedyna własność, na której stoi przypinanie:
    element pod indeksem `i` w wyniku `candidate_ids_of` to ten sam element,
    który wskaże `locator.nth(i)`. Lista nie może być nigdy przesortowana.
    """

    await page.set_content(
        """
        <div role="group" aria-label="Grupa A">
          <div class="row"><button>A1</button></div>
          <div class="row"><button>A2</button></div>
        </div>
        <div role="group" aria-label="Grupa B">
          <div class="row"><button>B1</button></div>
          <div class="row"><button>B2</button></div>
        </div>
        """
    )

    scoped = page.get_by_role("group", name="Grupa B").get_by_role("button")
    ids = await candidate_ids_of(scoped)

    assert len(ids) == await scoped.count() == 2
    assert len(set(ids)) == 2
    for index, expected in enumerate(ids):
        assert await candidate_ids_of(scoped.nth(index)) == [expected]

    # zawężenie faktycznie odcina drugą grupę
    everything = await candidate_ids_of(page.get_by_role("button"))
    assert len(everything) == 4
    assert set(ids) < set(everything)
