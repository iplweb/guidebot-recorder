"""`compile` reuse *validity*: staleness gating and positional-index drift.

The basic "reuse skips the reasoner / doesn't rewrite an unchanged sidecar"
happy paths live in ``test_compile_cache.py``. This file is about the signals
that *invalidate* reuse: an old compiler version or fingerprint, a step whose
kind changed, and a frozen positional `nth` that has drifted on the page.
"""

import textwrap

import pytest

from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import (
    compile_up_to_date,
    needs_positional_recheck,
    run_compile,
)
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled

from ._compile_helpers import SCENARIO, MockReasoner, make_page


@pytest.fixture
async def page():
    async for pg in make_page():
        yield pg


async def test_old_compiler_version_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    stale = compiled.model_copy(update={"compiler_version": COMPILER_VERSION - 1})
    write_compiled(cpath, stale)

    assert compile_up_to_date(path) is False


async def test_old_action_fingerprint_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    stale_fingerprint = action.fingerprint.model_copy(
        update={"compiler_version": COMPILER_VERSION - 1}
    )
    stale_action = action.model_copy(update={"fingerprint": stale_fingerprint})
    stale = compiled.model_copy(update={"actions": [None, stale_action]})
    write_compiled(cpath, stale)

    assert compile_up_to_date(path) is False


def test_targetless_scenario_requires_current_aligned_sidecar(tmp_path):
    path = tmp_path / "narration.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Narracja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - say: "Cześć"
            """
        ),
        encoding="utf-8",
    )
    cpath = compiled_path(path)

    assert compile_up_to_date(path) is False

    current = CompiledScenario(source=path.name, actions=[None])
    write_compiled(cpath, current)
    assert compile_up_to_date(path) is True

    write_compiled(cpath, current.model_copy(update={"source": "other.scenario.yaml"}))
    assert compile_up_to_date(path) is False

    write_compiled(cpath, current)

    write_compiled(
        cpath,
        current.model_copy(update={"compiler_version": COMPILER_VERSION - 1}),
    )
    assert compile_up_to_date(path) is False

    write_compiled(cpath, current.model_copy(update={"actions": []}))
    assert compile_up_to_date(path) is False


async def test_target_step_changed_to_say_requires_compile(tmp_path, page):
    path = tmp_path / "changed-kind.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    path.write_text(
        SCENARIO.replace('- teach: "kliknij Zaloguj"', '- say: "To już narracja"'),
        encoding="utf-8",
    )

    assert compile_up_to_date(path) is False


AMBIGUOUS_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Dwa przyciski
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<div><button>Usun</button></div><div><i>x</i></div><div><button>Usun</button></div>"
      - teach: "kliknij drugi Usun"
    """
)

#: Ta sama strona po przebudowie: drugi przycisk siedzi teraz w innym `<div>`
#: niż przy kompilacji, więc zamrożone `nth=1` trafia w element o **innej
#: pozycyjnej ścieżce DOM**. Kontrola tożsamości tego nie widzi — `tag` i
#: `ancestry_digest` (pary tag/rola, bez indeksów) są identyczne, co asertuje
#: sam test. Łańcuch przodków celowo zostaje ten sam: gdyby zmiana dokładała
#: `<section>` nad gałęzią, reuse padłby już na `ancestry_digest` i test nie
#: dowodziłby niczego o nowym sygnale.
#:
#: Jednorodne dołożenie trzeciego identycznego wiersza dryfu **nie** dałoby:
#: element trafiający w zamrożone `nth` zajmowałby tę samą pozycję strukturalną,
#: więc miałby ten sam skrót (spec, „Ograniczenie: co ten sygnał łapie, a czego
#: nie").
AMBIGUOUS_SCENARIO_MOVED = AMBIGUOUS_SCENARIO.replace(
    "<div><button>Usun</button></div><div><i>x</i></div><div><button>Usun</button></div>",
    "<div><button>Usun</button></div><div><button>Usun</button></div><div><i>x</i></div>",
)


class PickingReasoner:
    """Atrapa w nowym stylu: wskazuje kandydata, indeks liczy `compile`.

    Nie zwraca `nth` — dokładnie tak, jak po zmianie schematu widzianego przez
    model. Wybiera ostatniego kandydata o roli `button`, więc zamrożony indeks
    musi wyjść z pomiaru, a nie z arytmetyki na tablicy JSON.
    """

    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates, feedback=None):
        self.calls += 1
        buttons = [candidate for candidate in candidates if candidate.role == "button"]
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Usun", exact=True),
            candidate_id=buttons[-1].id,
        )


async def test_frozen_positional_index_needs_a_recheck_but_matches_the_source(tmp_path, page):
    """Namiar pozycyjny musi otworzyć przeglądarkę — inaczej dryf jest niewykrywalny.

    Odcisk kroku (`compiler_version`, `command_kind`, `compiled_from`,
    `config_hash`, `state`) nie zmienia się od przebudowy strony, a CLI kończy
    pracę na bramce kompilacji. Pyta o to jednak osobny predykat: `nth` nie robi
    sidecara *niezgodnym ze źródłem*, a zlanie obu pytań w `compile_up_to_date`
    unieruchamiało `render-set` (świeży sidecar wiecznie „nieaktualny").
    """

    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    # regresja: cache bez `nth` nadal oszczędza uruchomienie przeglądarki
    assert compile_up_to_date(path) is True
    assert needs_positional_recheck(path) is False

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    pinned = action.model_copy(update={"target": action.target.model_copy(update={"nth": 1})})
    write_compiled(cpath, compiled.model_copy(update={"actions": [None, pinned]}))

    assert needs_positional_recheck(path) is True
    # ale sidecar nadal odpowiada źródłu — to jest pytanie preflightu renderu
    assert compile_up_to_date(path) is True


async def test_frozen_positional_index_inside_scope_needs_a_recheck(tmp_path, page):
    """`nth` bywa na targecie zagnieżdżonym w `scope` — szukamy rekurencyjnie."""

    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    scoped = action.target.model_copy(
        update={"scope": RoleTarget(role="group", name="Formularz", nth=2)}
    )
    write_compiled(
        cpath,
        compiled.model_copy(
            update={"actions": [None, action.model_copy(update={"target": scoped})]}
        ),
    )

    assert needs_positional_recheck(path) is True
    assert compile_up_to_date(path) is True


async def test_positional_target_is_reused_while_the_page_holds_still(tmp_path, page):
    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")

    first = PickingReasoner()
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    action = load_compiled(compiled_path(path)).actions[1]
    assert action.target.nth == 1  # zmierzony, nie zgadnięty
    assert action.identity.dom_path_digest is not None

    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 0  # brak dryfu → reuse jak dotąd


async def test_legacy_pinned_sidecar_is_remeasured_once_and_then_heals(tmp_path, page):
    """Stary sidecar z **zgadniętym** `nth` nie może zostać zamrożony na zawsze.

    Brak `dom_path_digest` to podpis artefaktu sprzed tej zmiany — czyli tego,
    w którym indeks pochodził z arytmetyki modelu na tablicy JSON (zgłoszenie
    #51). Bezpieczny werdykt to „zmierz jeszcze raz"; po jednym przemierzeniu
    ścieżka jest zamrożona i kolejne kompilacje porównują ją normalnie.
    """

    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")
    await run_compile(path, page, PickingReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    legacy = action.model_copy(
        update={"identity": action.identity.model_copy(update={"dom_path_digest": None})}
    )
    write_compiled(cpath, compiled.model_copy(update={"actions": [None, legacy]}))

    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 1  # jednorazowe przemierzenie
    healed = load_compiled(cpath).actions[1]
    assert healed.identity.dom_path_digest is not None

    third = PickingReasoner()
    await run_compile(path, page, third, selects=None)

    assert third.calls == 0  # ścieżka już jest — reuse jak dla każdego innego wpisu


async def test_positional_drift_invalidates_reuse_and_reresolves(tmp_path, page):
    """Przebudowa strony nie rusza odcisku kroku, więc dryf jest jedynym sygnałem."""

    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")
    await run_compile(path, page, PickingReasoner(), selects=None)
    before = load_compiled(compiled_path(path)).actions[1]

    path.write_text(AMBIGUOUS_SCENARIO_MOVED, encoding="utf-8")
    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 1  # dryf unieważnił wpis
    after = load_compiled(compiled_path(path)).actions[1]
    assert after.identity.dom_path_digest != before.identity.dom_path_digest
    # Sama kontrola tożsamości przepuściłaby ten wpis: `tag` i `ancestry_digest`
    # się nie zmieniły, więc `reuse_is_valid` mówiło „ważny".
    assert after.identity.ancestry_digest == before.identity.ancestry_digest


async def test_positional_target_warns_with_the_match_count(tmp_path, page, capsys):
    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")

    await run_compile(path, page, PickingReasoner(), selects=None)

    out = capsys.readouterr().out
    # Liczebnik 1-based dla czytelnika, surowe `nth` obok — żeby autor odnalazł
    # ten sam wpis w sidecarze. Zamrożone `nth=1` to drugie z dwóch trafień.
    assert "namiar pozycyjny (2 z 2 pasujących, nth=1)" in out
    assert "rozważ doprecyzowanie opisu" in out
    assert "⚠ krok 2/2 — " in out
    assert "ambig.scenario.yaml:7" in out


async def test_unambiguous_target_does_not_warn(tmp_path, page, capsys):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    await run_compile(path, page, MockReasoner(), selects=None)

    assert "namiar pozycyjny" not in capsys.readouterr().out
