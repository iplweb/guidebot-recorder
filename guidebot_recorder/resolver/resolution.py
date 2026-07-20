"""Resolve a scenario step's target — the one seam shared by `compile` and `render`.

`compile` freezes targets ahead of time; `render` has to resolve in place when it
meets a branch that was never compiled (a :class:`PendingAction`).  Both need the
identical pipeline — collect candidates, ask the Reasoner, re-prompt, validate,
reject sensitive fields, freeze identity — so it lives here rather than inline in
either loop.

The root is deliberately ``Page | Frame``: with chrome enabled the active render
context is the shell's site iframe, and collecting candidates from the shell
instead would resolve against the wrong document.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Frame, Locator, Page

from guidebot_recorder.models.action import (
    ActionKind,
    Expect,
    WaitState,
    validate_teach_input_text,
)
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.scenario import Step, WaitUntil
from guidebot_recorder.models.target import Target
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import collect_candidates
from guidebot_recorder.resolver.reasoner import (
    ErrorReason,
    Reasoner,
    ReasonerError,
    ReasonerResult,
)
from guidebot_recorder.resolver.validate import (
    ValidationOk,
    is_sensitive_type_target,
    validate_compile_time,
)

#: how many times the Reasoner may be asked again after a rejected answer
MAX_REPROMPT = 2

#: the only resolver verdicts that mean "the element is not on the page".
#: ``multiple_actions`` is deliberately excluded — an ambiguous description is an
#: authoring error, and swallowing it would let a typo delete a branch silently.
ABSENT_REASONS: frozenset[ErrorReason] = frozenset({"no_action", "no_handle"})


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A validated target, ready to act on and to freeze into a ``CachedAction``."""

    action: ActionKind
    target: Target
    locator: Locator
    input_text: str | None
    state: WaitState | None
    identity: Identity | None


@dataclass(frozen=True, slots=True)
class TargetAbsent:
    """The Reasoner reported that the element simply is not there."""

    reason: ErrorReason
    message: str

    @property
    def error_message(self) -> str:
        """The message a caller raises with when the step was not optional."""

        return f"reasoner: {self.reason}: {self.message}"


def step_instruction(step: Step) -> str:
    """The natural-language text the Reasoner resolves for this step."""

    kind = step.command_kind()
    if kind == "teach":
        return step.teach
    if kind == "click":
        return step.click
    if kind == "hover":
        return step.hover
    if kind == "enterText":
        return step.enter_text.into
    if kind == "wait" and isinstance(step.wait, WaitUntil):
        return step.wait.until
    raise ValueError(f"krok bez instrukcji do rozwiązania: {kind}")


def action_for(kind: str, resolved: ActionKind) -> ActionKind:
    """Map a command kind onto the action to perform."""

    if kind == "teach":
        return resolved  # click / hover / type — inferred by the LLM
    if kind == "click":
        return "click"
    if kind == "hover":
        return "hover"
    if kind == "enterText":
        return "type"
    if kind == "wait":
        return "waitFor"
    raise ValueError(f"krok bez akcji: {kind}")


def step_state(step: Step) -> WaitState | None:
    """The wait state a ``wait: {until: ...}`` step expects, if any."""

    return step.wait.state if isinstance(step.wait, WaitUntil) else None


def heuristic_expect(url_before: str, url_after: str) -> Expect:
    return "navigation" if url_before != url_after else "none"


async def resolve_step_target(
    root: Page | Frame,
    step: Step,
    kind: str,
    reasoner: Reasoner,
) -> ResolvedTarget | TargetAbsent:
    """Resolve and validate ``step``'s target against the live ``root``.

    Returns :class:`TargetAbsent` only for the narrow verdicts in
    :data:`ABSENT_REASONS`; every other failure — an ambiguous description, an
    invented ``inputText``, a sensitive field, a target that never validates —
    raises ``RuntimeError``, because those are authoring or resolver bugs rather
    than a missing element.
    """

    instruction = step_instruction(step)
    candidates = await collect_candidates(root)
    resolution_error: str | None = None

    for _ in range(MAX_REPROMPT):
        result = await reasoner.resolve(instruction, candidates)
        if isinstance(result, ReasonerError):
            if result.reason in ABSENT_REASONS:
                return TargetAbsent(reason=result.reason, message=result.message)
            raise RuntimeError(f"reasoner: {result.reason}: {result.message}")
        assert isinstance(result, ReasonerResult)

        action = action_for(kind, result.action)
        infers_text = action == "type" and kind == "teach"
        input_text = result.input_text if infers_text else None
        if infers_text:
            if not isinstance(input_text, str):
                resolution_error = (
                    "reasoner nie zwrócił niepustego inputText dla akcji teach → type"
                )
                continue
            try:
                validate_teach_input_text(instruction, input_text)
            except ValueError as exc:
                resolution_error = str(exc)
                continue

        resolution_error = None
        validation = await validate_compile_time(root, result.target, action)
        if not isinstance(validation, ValidationOk):
            continue
        if infers_text and await is_sensitive_type_target(validation.locator):
            resolution_error = (
                "pole wygląda na przeznaczone dla wartości wrażliwej; użyj enterText z ENV"
            )
            continue

        state = step_state(step)
        # freeze identity BEFORE the action (the DOM may change); waitFor:hidden has none
        identity = (
            None
            if action == "waitFor" and state == "hidden"
            else await capture_identity(validation.locator)
        )
        return ResolvedTarget(
            action=action,
            target=result.target,
            locator=validation.locator,
            input_text=input_text,
            state=state,
            identity=identity,
        )

    if resolution_error is not None:
        raise RuntimeError(f"{resolution_error} po {MAX_REPROMPT} próbach")
    raise RuntimeError(f"nie udało się zwalidować namiaru dla: {instruction!r}")
