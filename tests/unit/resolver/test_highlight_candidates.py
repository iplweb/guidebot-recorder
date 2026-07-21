"""Krok `highlight` musi widzieć kontenery — inaczej „zakreśl obszar" jest fikcją.

Testy celowo NIE każą mockowi wymyślać odpowiedzi: sprawdzają, co Reasoner
w ogóle *dostaje*. Mock, który zwraca `RoleTarget(role="table")` z powietrza,
przechodzi nawet wtedy, gdy prawdziwy model nie ma z czego takiego celu zbudować.
"""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.scenario import Step
from guidebot_recorder.resolver.page_context import (
    CANDIDATE_ROLES,
    HIGHLIGHT_CANDIDATE_ROLES,
    collect_candidates,
)
from guidebot_recorder.resolver.reasoner import ReasonerError
from guidebot_recorder.resolver.resolution import TargetAbsent, resolve_step_target

PAGE = """
<h1>Raport</h1>
<button>Zapisz</button>
<table aria-label="Wyniki"><tr><td>12</td></tr></table>
<form aria-label="Filtry"><input aria-label="Rok"></form>
<section aria-label="Podsumowanie">wynik: 30</section>
"""


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        await pg.set_content(PAGE)
        yield pg
        await browser.close()


class RecordingReasoner:
    """Zapisuje kandydatów i zawsze mówi „nie ma" — nie udaje sukcesu."""

    def __init__(self):
        self.seen = []

    async def resolve(self, instruction, candidates):
        self.seen = list(candidates)
        return ReasonerError("no_action", "test nie wybiera celu")


async def test_default_candidates_do_not_include_containers(page):
    """Pin na dotychczasowe zachowanie: pozostałe komendy widzą to, co widziały."""

    roles = {c.role for c in await collect_candidates(page)}

    assert "button" in roles
    assert "table" not in roles
    assert "form" not in roles


async def test_highlight_roles_add_containers_without_dropping_controls(page):
    roles = {c.role for c in await collect_candidates(page, roles=HIGHLIGHT_CANDIDATE_ROLES)}

    assert {"table", "form", "region"} <= roles
    assert {"button", "heading"} <= roles


async def test_containers_keep_their_accessible_name(page):
    """Bez nazwy Reasoner mógłby wskazać kontener tylko pozycyjnie."""

    by_role = {
        c.role: c.name for c in await collect_candidates(page, roles=HIGHLIGHT_CANDIDATE_ROLES)
    }

    assert by_role["table"] == "Wyniki"
    assert by_role["form"] == "Filtry"


async def test_highlight_step_offers_containers_to_the_reasoner(page):
    """Sedno sprawy: `highlight: "tabela z wynikami"` musi mieć co wskazać."""

    reasoner = RecordingReasoner()

    result = await resolve_step_target(
        page, Step.model_validate({"highlight": "tabela z wynikami"}), "highlight", reasoner
    )

    assert isinstance(result, TargetAbsent)  # mock nie wybiera; liczy się, co zobaczył
    assert "table" in {c.role for c in reasoner.seen}


async def test_other_commands_still_see_only_controls(page):
    """Rozszerzenie jest zawężone do `highlight` — `click` dostaje to, co dotąd."""

    reasoner = RecordingReasoner()

    await resolve_step_target(page, Step.model_validate({"click": "Zapisz"}), "click", reasoner)

    assert "table" not in {c.role for c in reasoner.seen}
    assert set(CANDIDATE_ROLES) >= {c.role for c in reasoner.seen}
