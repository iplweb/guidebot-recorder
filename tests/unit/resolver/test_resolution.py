"""The shared target-resolution seam used by both `compile` and `render`."""

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.scenario import EnterText, Step, WaitUntil
from guidebot_recorder.models.target import RoleTarget
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
