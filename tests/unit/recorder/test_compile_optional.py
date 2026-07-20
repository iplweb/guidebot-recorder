"""`compile` behaviour for optional branches (`when:` blocks and `optional: true`)."""

import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, PendingAction
from guidebot_recorder.models.target import RoleTarget, TextTarget
from guidebot_recorder.recorder.compile import compile_up_to_date, run_compile
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled

BRANCH_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Bramka
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - when: "baner cookies"
        timeout: 1
        steps:
          - say: "Akceptujemy ciasteczka."
          - teach: "kliknij Akceptuję"
      - teach: "kliknij Zaloguj"
    """
)

OPTIONAL_STEP_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Krok opcjonalny
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - teach: "kliknij Akceptuję"
        optional: true
      - teach: "kliknij Zaloguj"
    """
)


class BranchReasoner:
    """Absent for anything but the login button."""

    def __init__(self, reason="no_handle"):
        self.instructions = []
        self.reason = reason

    async def resolve(self, instruction, candidates):
        self.instructions.append(instruction)
        if "Zaloguj" in instruction:
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="button", name="Zaloguj", exact=True),
            )
        return ReasonerError(reason=self.reason, message="nie widzę takiego elementu")


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_absent_gate_records_pending_for_gate_and_children(tmp_path, page, capsys):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(BRANCH_SCENARIO, encoding="utf-8")
    reasoner = BranchReasoner()

    await run_compile(path, page, reasoner)  # returns normally — no raise

    compiled = load_compiled(compiled_path(path))
    assert len(compiled.actions) == 5  # flat: navigate, gate, say, teach, teach
    assert compiled.actions[0] is None  # navigate
    assert isinstance(compiled.actions[1], PendingAction)  # gate
    assert compiled.actions[2] is None  # say child needs no target
    assert isinstance(compiled.actions[3], PendingAction)  # target-bearing child
    assert isinstance(compiled.actions[4], CachedAction)  # step after the branch still compiles

    # the branch children were never resolved or executed
    assert reasoner.instructions == ["baner cookies", "kliknij Zaloguj"]
    assert "baner cookies" in capsys.readouterr().out


async def test_pending_fingerprint_tracks_the_source_instruction(tmp_path, page):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(BRANCH_SCENARIO, encoding="utf-8")

    await run_compile(path, page, BranchReasoner())

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions[1].fingerprint.compiled_from == "baner cookies"
    assert compiled.actions[1].fingerprint.command_kind == "wait"
    assert compiled.actions[1].fingerprint.state == "visible"
    assert compiled.actions[3].fingerprint.compiled_from == "kliknij Akceptuję"


async def test_pending_entry_counts_as_up_to_date(tmp_path, page):
    """Otherwise every compile burns the full gate timeout on an absent banner."""

    path = tmp_path / "gate.scenario.yaml"
    path.write_text(BRANCH_SCENARIO, encoding="utf-8")
    await run_compile(path, page, BranchReasoner())

    assert compile_up_to_date(path) is True

    second = BranchReasoner()
    await run_compile(path, page, second)
    assert second.instructions == []  # nothing re-resolved


async def test_force_reattempts_a_pending_entry(tmp_path, page):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(BRANCH_SCENARIO, encoding="utf-8")
    await run_compile(path, page, BranchReasoner())

    assert compile_up_to_date(path, force=True) is False

    second = BranchReasoner()
    await run_compile(path, page, second, force=True)
    assert "baner cookies" in second.instructions


async def test_optional_step_records_pending_and_later_steps_still_compile(
    tmp_path, page, capsys
):
    path = tmp_path / "opt.scenario.yaml"
    path.write_text(OPTIONAL_STEP_SCENARIO, encoding="utf-8")
    reasoner = BranchReasoner()

    await run_compile(path, page, reasoner)

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions[0] is None
    assert isinstance(compiled.actions[1], PendingAction)
    assert isinstance(compiled.actions[2], CachedAction)
    assert "kliknij Akceptuję" in capsys.readouterr().out


async def test_multiple_actions_on_an_optional_step_is_still_fatal(tmp_path, page):
    """An ambiguous description is an authoring error, not an absent element."""

    path = tmp_path / "opt.scenario.yaml"
    path.write_text(OPTIONAL_STEP_SCENARIO, encoding="utf-8")

    with pytest.raises(RuntimeError, match="multiple_actions"):
        await run_compile(path, page, BranchReasoner(reason="multiple_actions"))


async def test_absent_target_on_a_required_step_still_fails(tmp_path, page):
    path = tmp_path / "req.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Wymagany
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<button>Zaloguj</button>"
              - teach: "kliknij Akceptuję"
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no_handle"):
        await run_compile(path, page, BranchReasoner())


async def test_present_gate_compiles_gate_and_children(tmp_path, page):
    path = tmp_path / "present.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Bramka obecna
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<div role=banner>Ciasteczka</div><button>Zaloguj</button>"
              - when: "baner cookies"
                timeout: 1
                steps:
                  - teach: "kliknij Zaloguj"
            """
        ),
        encoding="utf-8",
    )

    class PresentReasoner:
        async def resolve(self, instruction, candidates):
            if "baner" in instruction:
                return ReasonerResult(
                    action="waitFor", target=TextTarget(text="Ciasteczka", exact=True)
                )
            return ReasonerResult(
                action="click", target=RoleTarget(role="button", name="Zaloguj", exact=True)
            )

    await run_compile(path, page, PresentReasoner())

    compiled = load_compiled(compiled_path(path))
    assert isinstance(compiled.actions[1], CachedAction)
    assert compiled.actions[1].action == "waitFor"
    assert isinstance(compiled.actions[2], CachedAction)
