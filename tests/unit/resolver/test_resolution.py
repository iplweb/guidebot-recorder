"""The shared target-resolution seam used by both `compile` and `render`."""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.scenario import EnterText, Select, Step, WaitUntil
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    TargetAbsent,
    action_for,
    heuristic_expect,
    resolve_step_target,
    step_instruction,
    step_state,
)

_PAGE = "data:text/html,<button>Zaloguj</button>"
_FRAME_PAGE = (
    "data:text/html,<iframe srcdoc='" + "<button>Zaloguj</button>" + "' width=400 height=300>"
    "</iframe>"
)


class StubReasoner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


def test_step_instruction_covers_every_target_kind():
    assert step_instruction(Step(teach="kliknij")) == "kliknij"
    assert step_instruction(Step(click="przycisk")) == "przycisk"
    assert step_instruction(Step(hover="menu")) == "menu"
    assert step_instruction(Step(enter_text=EnterText(into="pole", text="x"))) == "pole"
    assert step_instruction(Step(wait=WaitUntil(until="baner"))) == "baner"
    with pytest.raises(ValueError):
        step_instruction(Step(say="cześć"))


def test_action_for_maps_command_kinds():
    assert action_for("teach", "type") == "type"
    assert action_for("click", "click") == "click"
    assert action_for("hover", "click") == "hover"
    assert action_for("enterText", "click") == "type"
    assert action_for("wait", "click") == "waitFor"
    with pytest.raises(ValueError):
        action_for("say", "click")


def test_heuristic_expect_and_step_state():
    assert heuristic_expect("a", "b") == "navigation"
    assert heuristic_expect("a", "a") == "none"
    assert step_state(Step(wait=WaitUntil(until="x", state="hidden"))) == "hidden"
    assert step_state(Step(click="x")) is None


async def test_resolve_returns_locator_and_identity(page):
    await page.goto(_PAGE)
    reasoner = StubReasoner(
        ReasonerResult(action="click", target=RoleTarget(role="button", name="Zaloguj", exact=True))
    )

    resolved = await resolve_step_target(page, Step(click="kliknij Zaloguj"), "click", reasoner)

    assert isinstance(resolved, ResolvedTarget)
    assert resolved.action == "click"
    assert resolved.identity is not None
    assert resolved.identity.tag == "button"
    assert await resolved.locator.count() == 1


async def test_resolve_accepts_a_frame_not_only_a_page(page):
    """Render with chrome enabled resolves against the site iframe's Frame."""

    await page.goto(_FRAME_PAGE)
    frame = page.frames[1]
    reasoner = StubReasoner(
        ReasonerResult(action="click", target=RoleTarget(role="button", name="Zaloguj", exact=True))
    )

    resolved = await resolve_step_target(frame, Step(click="kliknij Zaloguj"), "click", reasoner)

    assert isinstance(resolved, ResolvedTarget)
    assert await resolved.locator.count() == 1


@pytest.mark.parametrize("reason", ["no_action", "no_handle"])
async def test_absent_reasons_are_reported_not_raised(page, reason):
    await page.goto(_PAGE)
    reasoner = StubReasoner(ReasonerError(reason=reason, message="nie ma"))

    outcome = await resolve_step_target(page, Step(click="kliknij Zaloguj"), "click", reasoner)

    assert isinstance(outcome, TargetAbsent)
    assert outcome.reason == reason
    assert outcome.error_message == f"reasoner: {reason}: nie ma"


async def test_multiple_actions_is_a_hard_error(page):
    """An ambiguous description is an authoring bug, never an absent element."""

    await page.goto(_PAGE)
    reasoner = StubReasoner(ReasonerError(reason="multiple_actions", message="dwa"))

    with pytest.raises(RuntimeError, match="multiple_actions"):
        await resolve_step_target(page, Step(click="kliknij Zaloguj"), "click", reasoner)


async def test_unvalidatable_target_raises_after_reprompts(page):
    await page.goto(_PAGE)
    reasoner = StubReasoner(
        ReasonerResult(action="click", target=RoleTarget(role="button", name="Nie ma", exact=True))
    )

    with pytest.raises(RuntimeError, match="nie udało się zwalidować"):
        await resolve_step_target(page, Step(click="kliknij Zaloguj"), "click", reasoner)
    assert reasoner.calls == 2


#: two unnamed comboboxes, exactly as multiseek renders them: the resolver can
#: only tell them apart positionally, and the *first* one is the wrong one.
_TWO_SELECTS = """
    <select><option>Raport jednostki</option><option>Raport autora</option></select>
    <select><option>Artykuł w czasopismie</option><option>Rozdział</option></select>
"""

_SELECT_STEP = Step(
    select=Select(
        from_="lista wyboru wartości charakteru formalnego w ramce",
        option="Artykuł w czasopismie",
    )
)


async def test_select_step_rejects_a_dropdown_that_lacks_the_wanted_option(page):
    """The wrong `<select>` must be refused during validation, never handed to the recorder."""

    await page.set_content(_TWO_SELECTS)
    reasoner = StubReasoner(
        ReasonerResult(action="select", target=RoleTarget(role="combobox", name="", nth=0))
    )

    with pytest.raises(RuntimeError, match="Artykuł w czasopismie"):
        await resolve_step_target(page, _SELECT_STEP, "select", reasoner)
    assert reasoner.calls == 2


async def test_select_step_reprompt_recovers_the_dropdown_that_has_the_option(page):
    """A rejected candidate leaves the loop free to land on the right control."""

    await page.set_content(_TWO_SELECTS)
    reasoner = StubReasoner(
        ReasonerResult(action="select", target=RoleTarget(role="combobox", name="", nth=0)),
        ReasonerResult(action="select", target=RoleTarget(role="combobox", name="", nth=1)),
    )

    resolved = await resolve_step_target(page, _SELECT_STEP, "select", reasoner)

    assert isinstance(resolved, ResolvedTarget)
    assert reasoner.calls == 2
    assert await resolved.locator.evaluate("el => el.options[0].textContent.trim()") == (
        "Artykuł w czasopismie"
    )


async def test_exhausted_reprompts_report_why_the_last_candidate_was_rejected(page):
    """The bare "nie udało się zwalidować" told the author nothing actionable."""

    await page.goto(_PAGE)
    reasoner = StubReasoner(
        ReasonerResult(action="click", target=RoleTarget(role="button", name="Nie ma", exact=True))
    )

    with pytest.raises(RuntimeError, match="ostatnie odrzucenie") as excinfo:
        await resolve_step_target(page, Step(click="kliknij Zaloguj"), "click", reasoner)

    message = str(excinfo.value)
    assert "nie udało się zwalidować" in message
    assert "matched no elements" in message


async def test_select_step_option_rejection_is_reported_and_is_case_sensitive(page):
    """An option differing only in case is refused, and the message says so.

    Two rules meet here and neither may be relaxed into the other: the label
    comparison is exact (the one rule every execution path applies), and a
    rejection is *reported*, so the author reads which labels the element really
    offers instead of a bare "could not validate the target".
    """

    await page.set_content(_TWO_SELECTS)
    step = Step(select=Select(from_="lista charakteru formalnego", option="artykuł W CZASOPISMIE"))
    reasoner = StubReasoner(
        ReasonerResult(action="select", target=RoleTarget(role="combobox", name="", nth=1))
    )

    with pytest.raises(RuntimeError, match="ostatnie odrzucenie") as excinfo:
        await resolve_step_target(page, step, "select", reasoner)

    message = str(excinfo.value)
    assert "artykuł W CZASOPISMIE" in message  # what the scenario asked for
    assert "Artykuł w czasopismie" in message  # what the element really offers


async def test_select_step_without_a_visible_control_keeps_its_own_diagnosis(page):
    """The "nothing filmable here" message outranks the generic rejection report.

    Both were added independently — one on this branch, one upstream — and both
    now fire for the same candidate. The specific one has to win: "the page hid
    the <select> and nothing stands in for it" tells the author what to change,
    where "the target could not be validated (last rejection: not visible)" does
    not.
    """

    # `display: none` keeps the select out of the accessibility tree, so the
    # reasoner could only ever have reached it structurally.
    await page.set_content(
        '<select data-testid="woj" style="display:none"><option>Mazowieckie</option></select>'
    )
    step = Step(select=Select(from_="lista województw", option="Mazowieckie"))
    reasoner = StubReasoner(ReasonerResult(action="select", target=TestidTarget(testid="woj")))

    with pytest.raises(RuntimeError) as excinfo:
        await resolve_step_target(page, step, "select", reasoner)

    message = str(excinfo.value)
    assert "nie znaleziono widocznej kontrolki" in message
    assert "ostatnie odrzucenie" not in message


async def test_hidden_wait_captures_no_identity(page):
    await page.goto(_PAGE)
    reasoner = StubReasoner(
        ReasonerResult(
            action="waitFor", target=RoleTarget(role="button", name="Zaloguj", exact=True)
        )
    )

    resolved = await resolve_step_target(
        page, Step(wait=WaitUntil(until="Zaloguj", state="hidden")), "wait", reasoner
    )

    assert isinstance(resolved, ResolvedTarget)
    assert resolved.state == "hidden"
    assert resolved.identity is None
