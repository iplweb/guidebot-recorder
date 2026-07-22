"""`pin_position` / `pinned_drifted` on real Chromium — the machine counts nth.

The fixture reproduces the evidence of issue #51: a django-multiseek-style
criteria form with repeating rows. The trap the reviewer flagged is that
``<button>×</button>`` *has* the accessible name ``"×"`` (``accessibleName``
takes the ``textAlternative`` of a ``button``), so an unnamed delete button must
wrap its icon in an ``aria-hidden`` span. The genuinely unnamed controls in the
issue were textboxes (``role=textbox name=''``), so the "Zakres lat" (year range)
row carries two of them — the pair that got the *same* ``nth=1`` in the bug.
"""

import pytest
from playwright.async_api import async_playwright

import guidebot_recorder.resolver.positional as positional
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget, TextTarget
from guidebot_recorder.recorder.compile import _carries_positional_index
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import candidate_ids_of
from guidebot_recorder.resolver.positional import (
    PinFail,
    Pinned,
    pin_position,
    pinned_drifted,
)
from guidebot_recorder.resolver.validate import build_locator

#: Three rows with an unnamed delete button (icon hidden from the a11y tree),
#: a "Zakres lat" row with two unnamed textboxes, and one uniquely named button.
_FORM = """
<form>
  <div class="row"><span>Kryterium 1</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Kryterium 2</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Kryterium 3</span>
    <button class="del"><span aria-hidden="true">×</span></button></div>
  <div class="row"><span>Zakres lat</span>
    <input type="text" class="rok"><input type="text" class="rok"></div>
  <button>Szukaj</button>
</form>
"""

_UNNAMED_BUTTON = RoleTarget(role="button", name="", exact=True)
_UNNAMED_TEXTBOX = RoleTarget(role="textbox", name="", exact=True)


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


def _cached(target, identity: Identity | None, action: str = "click") -> CachedAction:
    return CachedAction(
        action=action,
        target=target,
        identity=identity,
        expect="none",
        fingerprint=Fingerprint(
            command_kind="teach",
            compiled_from="usuń kryterium",
            expect="none",
            config_hash="config",
        ),
    )


async def _ids_of(page, target) -> list[str]:
    """The candidate ids the caller would hand `pin_position`, in `.nth(i)` order."""

    return await candidate_ids_of(await build_locator(page, target))


async def _returning(ids: list[str]) -> list[str]:
    """Podstawiony odczyt ścieżek — DOM, którego przeglądarka nie potrafi udać."""

    return ids


# --- pin_position: the spec rule table -------------------------------------


async def test_single_match_is_pinned_without_nth(page):
    await page.set_content(_FORM)

    result = await pin_position(page, RoleTarget(role="button", name="Szukaj"), None)

    assert isinstance(result, Pinned)
    assert result.matches == 1
    assert result.index is None
    assert isinstance(result.target, RoleTarget)
    assert result.target.nth is None


async def test_three_matches_and_the_third_id_pins_nth_2(page):
    await page.set_content(_FORM)
    ids = await _ids_of(page, _UNNAMED_BUTTON)
    assert len(ids) == 3

    result = await pin_position(page, _UNNAMED_BUTTON, ids[2])

    assert isinstance(result, Pinned)
    assert result.matches == 3
    assert result.index == 2
    assert result.target.nth == 2


async def test_no_candidate_id_with_many_matches_fails(page):
    await page.set_content(_FORM)

    result = await pin_position(page, _UNNAMED_BUTTON, None)

    assert isinstance(result, PinFail)
    assert result.reason == "no_candidate_id"


async def test_candidate_id_absent_from_matches_fails(page):
    await page.set_content(_FORM)

    result = await pin_position(page, _UNNAMED_BUTTON, "candidate-0000000000000000")

    assert isinstance(result, PinFail)
    assert result.reason == "candidate_not_matched"
    # message safety: only numbers and the candidate id token, no page text
    assert "candidate-0000000000000000" in result.message


async def test_zero_matches_is_not_found(page):
    await page.set_content(_FORM)

    result = await pin_position(page, RoleTarget(role="button", name="Nieistniejący"), None)

    assert isinstance(result, PinFail)
    assert result.reason == "not_found"


async def test_non_role_target_is_not_pinnable(page):
    await page.set_content(_FORM)

    result = await pin_position(page, TextTarget(text="Zakres lat"), None)

    assert isinstance(result, PinFail)
    assert result.reason == "not_pinnable"


async def test_not_pinnable_tells_the_model_what_to_do_next(page):
    """Ten komunikat trafia do promptu — musi być poleceniem, nie żargonem.

    „target is not a positional (role) target" jest bezpieczne, ale bezużyteczne:
    model nie ma pojęcia, co z tym zrobić. W tej sytuacji ma usłyszeć, w ile
    elementów trafił namiar i czym go zawęzić. Kontrakt bez zmian: wyłącznie
    liczby i tokeny `candidate-<hex>`, zero tekstu ze strony.
    """

    await page.set_content(_FORM)

    result = await pin_position(page, TextTarget(text="Kryterium", exact=False), None)

    assert isinstance(result, PinFail)
    assert result.reason == "not_pinnable"
    assert "3" in result.message  # tyle wierszy zawiera „Kryterium"
    assert "scope" in result.message
    # nic ze strony: ani nazwa kontrolki, ani szukany tekst
    assert "Kryterium" not in result.message
    assert "positional (role) target" not in result.message


async def test_two_year_steps_get_different_nth(page):
    """The most telling regression from the issue: two distinct steps that each
    named a different candidate must pin to *different* indices, not share one."""

    await page.set_content(_FORM)
    ids = await _ids_of(page, _UNNAMED_TEXTBOX)
    assert len(ids) == 2
    assert ids[0] != ids[1]

    od = await pin_position(page, _UNNAMED_TEXTBOX, ids[0])
    do = await pin_position(page, _UNNAMED_TEXTBOX, ids[1])

    assert isinstance(od, Pinned) and isinstance(do, Pinned)
    assert od.target.nth == 0
    assert do.target.nth == 1
    assert od.target.nth != do.target.nth


# --- pinned_drifted --------------------------------------------------------


async def test_drift_false_for_a_freshly_frozen_pin(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    assert await pinned_drifted(page, cached) is False


async def test_drift_true_after_a_row_is_injected_before_the_target(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    # Inject a fresh unnamed button ahead of the first row's button. This shifts
    # the target's positional DOM path (a uniform same-tag row wrapper would not:
    # it would move every row down one nth-of-type in lockstep, leaving the path
    # at the frozen index structurally identical — and correctly undetected).
    await page.evaluate(
        """() => {
          const firstRow = document.querySelector('.row');
          const button = document.createElement('button');
          const icon = document.createElement('span');
          icon.setAttribute('aria-hidden', 'true');
          icon.textContent = '×';
          button.appendChild(icon);
          firstRow.insertBefore(button, firstRow.firstChild);
        }"""
    )

    assert await pinned_drifted(page, cached) is True


async def test_drift_true_when_the_match_list_shrank_below_nth(page):
    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(2))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 2}), identity)

    await page.evaluate(
        """() => {
          const dels = Array.from(document.querySelectorAll('button.del'));
          dels.slice(1).forEach((button) => button.closest('.row').remove());
        }"""
    )

    assert await pinned_drifted(page, cached) is True


async def test_drift_true_when_a_pinned_entry_has_no_identity(page):
    """Brak zamrożonej ścieżki przy `nth` to powód, żeby ZMIERZYĆ, nie żeby ufać."""

    await page.set_content(_FORM)
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), None)

    assert await pinned_drifted(page, cached) is True


async def test_drift_true_for_a_pinned_sidecar_predating_dom_paths(page):
    """Sidecar sprzed tej zmiany niesie **zgadnięty** `nth` — korpus ze zgłoszenia #51.

    Milczenie („nie zmyślamy werdyktu") było tu złym domyślnym: to dokładnie te
    artefakty, dla których cały mechanizm powstał, a bez unieważnienia zostałyby
    zamrożone na zawsze — reuse przechodził, reasoner nie był pytany ani razu.
    """

    await page.set_content(_FORM)
    identity = Identity(tag="button", ancestry_digest="whatever")  # dom_path_digest defaults None
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)

    assert cached.identity is not None and cached.identity.dom_path_digest is None
    assert await pinned_drifted(page, cached) is True


async def test_drift_ignores_an_index_buried_in_scope_and_the_compile_gate_catches_it(page):
    """Asymetria wobec `_carries_positional_index` jest świadoma i udokumentowana.

    `nth` w `scope` jest nieosiągalne od modelu (`_reject_index` blokuje je na
    każdym poziomie, a `pin_position` ustawia indeks wyłącznie na targecie
    zewnętrznym), więc taki wpis nie ma też zamrożonej ścieżki do porównania.
    Ręcznie zredagowany sidecar to osiągnie — i wtedy łapie go bramka
    kompilacji, która schodzi rekurencyjnie, bo tam fałszywy alarm kosztuje
    tylko uruchomienie przeglądarki.
    """

    await page.set_content(_FORM)
    scoped = RoleTarget(
        role="button",
        name="",
        exact=True,
        scope=RoleTarget(role="group", name="", nth=1),
    )
    identity = Identity(tag="button", ancestry_digest="a", dom_path_digest="p")

    assert await pinned_drifted(page, _cached(scoped, identity)) is False
    assert _carries_positional_index(_cached(scoped, identity)) is True


async def test_drift_true_when_the_page_went_away_mid_check(page):
    """Zamknięta strona to dryf, nie surowy wyjątek — symetrycznie do `reuse_failure`.

    `reuse_is_valid` i `pinned_drifted` to osobne ogniwa łańcucha `and`
    w `compile`, dzielone kilkunastoma round-tripami do przeglądarki. Strona,
    która zniknie między nimi, wywalała kompilację `TargetClosedError` — bez
    bannera `plik:linia`, którym `compile` opisuje każdy inny werdykt.
    """

    await page.set_content(_FORM)
    locator = await build_locator(page, _UNNAMED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_UNNAMED_BUTTON.model_copy(update={"nth": 1}), identity)
    assert identity.dom_path_digest is not None  # inaczej test nie dotknąłby przeglądarki

    await page.close()

    assert await pinned_drifted(page, cached) is True


async def test_drift_false_without_nth_even_when_the_path_is_missing(page):
    """Bez `nth` nie ma czego mierzyć — unieważnianie cache'u wszystkim byłoby szkodą."""

    await page.set_content(_FORM)
    identity = Identity(tag="button", ancestry_digest="whatever")
    cached = _cached(RoleTarget(role="button", name="Szukaj"), identity)

    assert await pinned_drifted(page, cached) is False


# --- fail-closed / dwa odczyty DOM -----------------------------------------


async def test_a_candidate_id_matching_several_elements_fails_closed(page, monkeypatch):
    """Jedyny bezpiecznik naprawy unikalności — dotąd bez żadnego testu.

    Gdyby ścieżka DOM okazała się nieunikalna (a właśnie tego pilnuje
    `page_context`), przypisanie „pierwszego trafienia" po cichu wskazałoby inny
    element niż model. Werdykt musi być odmowny.
    """

    await page.set_content(_FORM)
    collision = "candidate-1111111111111111"
    monkeypatch.setattr(
        positional, "candidate_ids_of", lambda locator: _returning([collision, collision, "x"])
    )

    result = await pin_position(page, _UNNAMED_BUTTON, collision)

    assert isinstance(result, PinFail)
    assert result.reason == "ambiguous_candidate_id"
    # kontrakt komunikatu: wyłącznie liczby i token `candidate-<hex>`
    assert collision in result.message
    assert "2" in result.message and "3" in result.message


async def test_matches_and_index_come_from_one_and_the_same_read(page, monkeypatch):
    """`count()` i `evaluate_all` to dwa odczyty — baner nie może mówić „3 z 2".

    DOM potrafi się zmienić między nimi. Liczba, którą raportujemy razem
    z indeksem, musi pochodzić z tego samego odczytu co indeks, inaczej
    ostrzeżenie dla autora jest wewnętrznie sprzeczne.
    """

    await page.set_content(_FORM)  # `count()` zobaczy trzy przyciski
    monkeypatch.setattr(
        positional, "candidate_ids_of", lambda locator: _returning(["a", "b"])
    )

    result = await pin_position(page, _UNNAMED_BUTTON, "b")

    assert isinstance(result, Pinned)
    assert result.index == 1
    assert result.matches == 2  # nie 3
    assert result.index < result.matches


# --- scope: namiar zawężony do gałęzi --------------------------------------

#: Dwie grupy z identycznymi, nienazwanymi przyciskami. Bez `scope` namiar trafia
#: w cztery elementy; ze `scope` — w dwa, i dopiero wtedy indeks cokolwiek znaczy.
_TWO_GROUPS = """
<div role="group" aria-label="Grupa A">
  <div class="row"><button><span aria-hidden="true">×</span></button></div>
  <div class="row"><button><span aria-hidden="true">×</span></button></div>
</div>
<div role="group" aria-label="Grupa B">
  <div class="row"><button><span aria-hidden="true">×</span></button></div>
  <div class="row"><button><span aria-hidden="true">×</span></button></div>
</div>
"""

_SCOPED_BUTTON = RoleTarget(
    role="button",
    name="",
    exact=True,
    scope=RoleTarget(role="group", name="Grupa B", exact=True),
)


async def test_scope_narrows_the_set_the_index_is_measured_against(page):
    """`scope` to druga połowa naprawy — indeks liczy się w obrębie zawężenia."""

    await page.set_content(_TWO_GROUPS)
    assert await (await build_locator(page, _UNNAMED_BUTTON)).count() == 4
    ids = await _ids_of(page, _SCOPED_BUTTON)
    assert len(ids) == 2

    result = await pin_position(page, _SCOPED_BUTTON, ids[1])

    assert isinstance(result, Pinned)
    assert result.matches == 2  # dwa, nie cztery
    assert result.index == 1
    assert result.target.scope == _SCOPED_BUTTON.scope  # zawężenie przeżywa przypięcie
    assert await (await build_locator(page, result.target)).count() == 1


async def test_drift_of_a_scoped_pin_is_measured_inside_its_scope(page):
    """Zamrożona ścieżka scoped celu jest porównywana z listą z tego samego zawężenia."""

    await page.set_content(_TWO_GROUPS)
    locator = await build_locator(page, _SCOPED_BUTTON)
    identity = await capture_identity(locator.nth(1))
    cached = _cached(_SCOPED_BUTTON.model_copy(update={"nth": 1}), identity)

    assert await pinned_drifted(page, cached) is False

    # przycisk dołożony do grupy A nie rusza zawężonej listy — brak dryfu
    await page.evaluate(
        """() => {
          const groupA = document.querySelector('[aria-label="Grupa A"]');
          groupA.insertBefore(document.createElement('button'), groupA.firstChild);
        }"""
    )
    assert await pinned_drifted(page, cached) is False

    # ten sam zabieg w grupie B przesuwa cel — dryf
    await page.evaluate(
        """() => {
          const groupB = document.querySelector('[aria-label="Grupa B"]');
          groupB.insertBefore(document.createElement('button'), groupB.firstChild);
        }"""
    )
    assert await pinned_drifted(page, cached) is True
