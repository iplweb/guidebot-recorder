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
    """The natural-language text the Reasoner resolves for this step.

    The author's own sentence and nothing else — see :func:`compiled_from` for
    the fingerprint, which is a different question with a different answer.
    """

    kind = step.command_kind()
    if kind == "teach":
        return step.teach
    if kind == "click":
        return step.click
    if kind == "hover":
        return step.hover
    if kind == "enterText":
        return step.enter_text.into
    if kind == "select":
        return step.select.from_
    if kind == "wait" and isinstance(step.wait, WaitUntil):
        return step.wait.until
    raise ValueError(f"krok bez instrukcji do rozwiązania: {kind}")


def compiled_from(step: Step) -> str:
    """The step content a frozen action is fingerprinted against.

    Everything the compiler resolved *from* has to be in here, or editing it
    leaves a stale sidecar looking current. That is more than the sentence the
    reasoner sees: a ``select:`` step also carries a per-step ``mode``, and
    deleting ``mode: native`` from one used to leave ``compile_up_to_date()``
    true — no browser opened, the drivability probe never ran, and the render
    drove a widget nothing had checked.

    Kept apart from :func:`step_instruction` on purpose, even though the two
    differ by one line. ``step_instruction`` is the prompt the Reasoner resolves
    against; folding a YAML keyword into that would hand the LLM ``mode:
    native`` as though it were part of the author's description of the control.
    The suffix is appended only when the step actually sets a mode, so every
    fingerprint frozen before this existed stays valid and no scenario needs a
    recompile for the change itself.
    """

    instruction = step_instruction(step)
    if step.command_kind() == "select" and step.select.mode is not None:
        return f"{instruction} [mode: {step.select.mode}]"
    return instruction


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
    if kind == "select":
        return "select"
    if kind == "wait":
        return "waitFor"
    raise ValueError(f"krok bez akcji: {kind}")


def step_state(step: Step) -> WaitState | None:
    """The wait state a ``wait: {until: ...}`` step expects, if any."""

    return step.wait.state if isinstance(step.wait, WaitUntil) else None


def heuristic_expect(url_before: str, url_after: str) -> Expect:
    return "navigation" if url_before != url_after else "none"


def _relaxed_exact(target: Target) -> Target | None:
    """A copy of a name-based target with exact matching relaxed, or ``None``.

    guidebot's accessible-name computation (``collect_candidates``) and
    Playwright's ``get_by_*`` matcher can disagree by insignificant whitespace —
    a decorative icon that is not ``aria-hidden`` leaves a leading space, a
    required-field asterisk is spaced differently — so an exact name the reasoner
    copied verbatim from a candidate can match nothing under Playwright. Relaxing
    ``exact`` lets the caller retry; the retry is accepted only when it still
    resolves *uniquely*, so nothing is loosened silently. Only role/text/label
    carry ``exact``; a test-id target has nothing to relax and yields ``None``.
    """

    if getattr(target, "exact", False) is True:
        return target.model_copy(update={"exact": False})
    return None


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
        target = result.target
        validation = await validate_compile_time(root, target, action)
        if not isinstance(validation, ValidationOk):
            # An exact name the reasoner copied from a candidate can miss under
            # Playwright's matcher over insignificant whitespace. Retry once with
            # exact matching relaxed, accepting it only if it still resolves
            # uniquely; persist the relaxed target so render agrees with compile.
            relaxed = _relaxed_exact(target)
            if relaxed is not None:
                relaxed_validation = await validate_compile_time(root, relaxed, action)
                if isinstance(relaxed_validation, ValidationOk):
                    target, validation = relaxed, relaxed_validation
            if not isinstance(validation, ValidationOk):
                if action == "select" and validation.reason == "not_visible":
                    # Not a reasoner miss to be re-prompted away silently: the
                    # page has no control a viewer could see for this select, so
                    # the choice cannot be filmed at all. Re-prompting may still
                    # find a *different* select, so the loop continues — but if
                    # it does not, the run must say which situation this is
                    # rather than "could not validate the target".
                    resolution_error = (
                        f"nie znaleziono widocznej kontrolki dla listy {instruction!r} "
                        "— strona ukryła <select> i nic widocznego go nie zastępuje, "
                        "więc nie da się pokazać wyboru na filmie"
                    )
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
            target=target,
            locator=validation.locator,
            input_text=input_text,
            state=state,
            identity=identity,
        )

    if resolution_error is not None:
        raise RuntimeError(f"{resolution_error} po {MAX_REPROMPT} próbach")
    raise RuntimeError(f"nie udało się zwalidować namiaru dla: {instruction!r}")
