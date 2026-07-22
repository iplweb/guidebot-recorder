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

import inspect
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

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
from guidebot_recorder.resolver.page_context import candidate_roles_for, collect_candidates
from guidebot_recorder.resolver.positional import PinFail, Pinned, pin_position
from guidebot_recorder.resolver.reasoner import (
    ErrorReason,
    Reasoner,
    ReasonerError,
    ReasonerResult,
)
from guidebot_recorder.resolver.validate import (
    ValidationFail,
    ValidationOk,
    is_sensitive_type_target,
    validate_compile_time,
)

#: how many times the Reasoner may be asked in total for one step. Three, not the
#: original two: `for _ in range(MAX_REPROMPT)` means "attempts altogether", so at
#: two the feedback built from a failed pin would get exactly one corrective shot
#: — too few for a harder task whose every failure is now a hard compile error.
MAX_REPROMPT = 3

#: the only resolver verdicts that mean "the element is not on the page".
#: ``multiple_actions`` is deliberately excluded — an ambiguous description is an
#: authoring error, and swallowing it would let a typo delete a branch silently.
ABSENT_REASONS: frozenset[ErrorReason] = frozenset({"no_action", "no_handle"})


class TargetResolutionError(RuntimeError):
    """A verdict of :func:`resolve_step_target` itself: the step cannot be resolved.

    Named rather than a bare ``RuntimeError`` so a caller can tell this apart
    from whatever an injected ``Reasoner`` raises through the same frame —
    :class:`~guidebot_recorder.recorder.session.SetupNeedsCompile` is a
    ``RuntimeError`` too, and it is control flow, not a verdict. Compile wraps
    only verdicts in a `plik:linia` banner; anything else must pass through with
    its own type intact.

    Subclasses ``RuntimeError``, so every existing ``except RuntimeError`` and
    ``pytest.raises(RuntimeError)`` keeps working.
    """


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A validated target, ready to act on and to freeze into a ``CachedAction``."""

    action: ActionKind
    target: Target
    locator: Locator
    input_text: str | None
    state: WaitState | None
    identity: Identity | None
    #: How the index in ``target`` was measured, when one was. ``None`` when the
    #: target resolved uniquely on its own — the common case. The default is
    #: mandatory: this is a ``frozen``/``slots`` dataclass built with keyword
    #: arguments in test doubles that know nothing of pinning. It carries the
    #: whole :class:`Pinned` rather than a bare flag because the compile banner
    #: has to say "2 of 11 matching", and neither a ``bool`` nor ``target.nth``
    #: can be turned back into a match count.
    pinned: Pinned | None = None


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
    if kind == "highlight":
        return step.highlight_config().what
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
    # `highlight` needs no such suffix, and that is a decision rather than an
    # omission: its knobs (`padding`/`loops`/`hold`/`color`) restyle the mark
    # without moving it, so a restyle must not invalidate the frozen target.
    return instruction


def action_for(kind: str, resolved: ActionKind) -> ActionKind:
    """Map a command kind onto the action to perform."""

    if kind == "teach":
        # click / hover / type — inferred by the LLM. `highlight` is not in the
        # Reasoner's vocabulary (REASONER_ACTIONS), so seeing one here means that
        # invariant broke somewhere; fail loudly rather than freeze a `highlight`
        # onto a step that carries none of its knobs.
        if resolved == "highlight":
            raise ValueError("reasoner zwrócił `highlight` dla kroku `teach` — akcja niedozwolona")
        return resolved
    if kind == "click":
        return "click"
    if kind == "highlight":
        return "highlight"
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


#: The only thing said back to the model about an unknown candidate id. Model
#: output may carry page text, so the id itself is *not* quoted here — echoing it
#: would smuggle text around the prompt's untrusted-data fence. A count is a
#: number, and numbers are what this channel is allowed to carry.
_UNKNOWN_CANDIDATE_FEEDBACK = (
    "the previous answer named a candidateId that was not among the {count} "
    "candidates provided; answer with an id taken from the candidate list"
)


def _takes_feedback(resolve: Any) -> bool:
    """Whether ``resolve`` would accept a ``feedback=`` keyword."""

    if resolve is None:  # pragma: no cover — the protocol requires it
        return False
    try:
        parameters = inspect.signature(resolve).parameters.values()
    except (TypeError, ValueError):  # pragma: no cover — exotic callables
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    return any(
        parameter.name == "feedback"
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for parameter in parameters
    )


@lru_cache(maxsize=256)
def _class_takes_feedback(reasoner_type: type[Any]) -> bool:
    """:func:`_takes_feedback` for a class, memoised — one ``signature`` call per type."""

    return _takes_feedback(getattr(reasoner_type, "resolve", None))


def _accepts_feedback(reasoner: object) -> bool:
    """Whether this ``Reasoner`` implementation's ``resolve`` takes ``feedback``.

    Roughly forty test doubles implement the protocol as
    ``resolve(self, instruction, candidates)`` with no ``**kwargs``. A default
    value on the real implementations is not enough to protect them: the moment
    a pin fails, the loop *wants* to pass feedback, and an old double would
    answer ``TypeError: got an unexpected keyword argument`` — not a
    :class:`TargetResolutionError`, so it escapes compile's `plik:linia` banner
    entirely.

    Asked of the signature rather than by catching ``TypeError`` around the call:
    a ``TypeError`` raised *inside* the double's own body would otherwise be
    swallowed and silently retried two-argument, turning a genuine bug into a
    confusing rerun.

    The class is consulted first — that is where the answer lives for every real
    implementation, and it is the key the cache is built on. An object carrying
    ``resolve`` per instance (a mock, a ``partial``) still gets a correct answer,
    just an uncached one.
    """

    if getattr(type(reasoner), "resolve", None) is not None:
        return _class_takes_feedback(type(reasoner))
    return _takes_feedback(getattr(reasoner, "resolve", None))


@dataclass(frozen=True, slots=True)
class _Accepted:
    """A target variant that validated, plus how its index was measured (if it was)."""

    target: Target
    validation: ValidationOk
    pinned: Pinned | None


async def _accept_variant(
    root: Page | Frame,
    target: Target,
    action: ActionKind,
    option: str | None,
    candidate_id: str | None,
    may_pin: bool,
) -> _Accepted | ValidationFail | PinFail:
    """Validate one target variant, measuring an index when that is what stands in the way.

    Returns the accepted variant, or the failure that stopped it: a
    :class:`ValidationFail` when the target does not resolve at all, or a
    :class:`PinFail` when it resolves to several elements and the index could not
    be measured — the one failure the model can act on, and the only one whose
    wording is safe to route back into a prompt.
    """

    validation = await validate_compile_time(root, target, action, option=option)
    if isinstance(validation, ValidationOk):
        return _Accepted(target=target, validation=validation, pinned=None)
    if validation.reason != "not_unique" or not may_pin:
        return validation

    pinned = await pin_position(root, target, candidate_id)
    if isinstance(pinned, PinFail):
        return pinned
    # `Pinned(index=None)` is reachable only through a DOM race — validation saw
    # ≥ 2 matches, the pin saw 1. It is a success like any other: revalidate the
    # target it produced (here, the `nth`-less one) and accept it if it stands.
    revalidated = await validate_compile_time(root, pinned.target, action, option=option)
    if isinstance(revalidated, ValidationOk):
        return _Accepted(target=pinned.target, validation=revalidated, pinned=pinned)
    return revalidated


@dataclass(frozen=True, slots=True)
class _NoVariantAccepted:
    """Why no variant of the reasoner's target validated, split by who may hear it.

    Both failures are carried because they answer different audiences, and the
    split is the point: ``rejection`` is human-facing and may contain page text,
    ``pin_failure`` is the only wording that may be routed back into a prompt.
    Reading them off one object is what stops the two from being confused —
    see :attr:`feedback`.
    """

    rejection: ValidationFail | None
    pin_failure: PinFail | None

    @property
    def feedback(self) -> str | None:
        """What may be said back to the model about this failure, if anything.

        Only a pin failure is safe to quote: it is built from match counts and
        ids this module minted. A ``ValidationFail.message`` may carry page text
        (``_option_missing_message`` pastes <option> labels straight from the
        page), so it never leaves the human-facing path.
        """

        return None if self.pin_failure is None else self.pin_failure.message


async def _accept_any_variant(
    root: Page | Frame,
    target: Target,
    action: ActionKind,
    option: str | None,
    candidate_id: str | None,
    may_pin: bool,
) -> _Accepted | _NoVariantAccepted:
    """Try the reasoner's target, then the same target with ``exact`` relaxed.

    An exact name the reasoner copied from a candidate can miss under
    Playwright's matcher over insignificant whitespace, so the relaxed variant is
    tried too — and it is a full variant, pinning included: "exact → not_found,
    relaxed → not_unique" is precisely the shape :func:`_relaxed_exact` exists
    for. Whichever variant passes is the one returned, relaxed or not, so render
    agrees with compile.

    When neither passes, the returned rejection is the **first** one, not the
    last: it says why the target the reasoner actually answered with was refused,
    rather than why a variant this code invented behind the reasoner's back was.
    A pin failure is taken from whichever variant produced one. Both directions
    are pinned by ``test_relaxed_exact_variant_that_resolves_uniquely_is_the_one_frozen``
    and ``test_failed_relaxed_retry_reports_the_original_rejection_not_the_relaxed_one``.
    """

    variants = [target]
    relaxed = _relaxed_exact(target)
    if relaxed is not None:
        variants.append(relaxed)

    rejection: ValidationFail | None = None
    pin_failure: PinFail | None = None
    for variant in variants:
        outcome = await _accept_variant(root, variant, action, option, candidate_id, may_pin)
        if isinstance(outcome, _Accepted):
            return outcome
        if isinstance(outcome, PinFail):
            pin_failure = outcome
        elif rejection is None:
            rejection = outcome
    return _NoVariantAccepted(rejection=rejection, pin_failure=pin_failure)


def _teach_input_text_error(instruction: str, input_text: str | None) -> str | None:
    """Why the model's ``inputText`` cannot be used for a ``teach`` step, or ``None``.

    ``teach`` is the only kind that lets the model invent the literal to type, so
    it is the only one whose answer has to be checked against the author's own
    sentence.
    """

    if not isinstance(input_text, str):
        return "reasoner nie zwrócił niepustego inputText dla akcji teach → type"
    try:
        validate_teach_input_text(instruction, input_text)
    except ValueError as exc:
        return str(exc)
    return None


def _exhausted_error(
    instruction: str,
    resolution_error: str | None,
    last_rejection: ValidationFail | None,
    last_pin_failure: PinFail | None,
) -> TargetResolutionError:
    """The verdict to raise once ``MAX_REPROMPT`` rounds produced nothing usable.

    Returned rather than raised so the traceback still points at
    :func:`resolve_step_target`, which is the frame an author has to look at.
    """

    if resolution_error is not None:
        return TargetResolutionError(f"{resolution_error} po {MAX_REPROMPT} próbach")
    message = f"nie udało się zwalidować namiaru dla: {instruction!r}"
    if last_rejection is not None:
        # Without this the author only learns *that* every candidate was refused.
        # The reason — an absent option, a non-select, an ambiguous name — is the
        # one piece of information that says what to fix in the scenario.
        message += f" (ostatnie odrzucenie: {last_rejection.message})"
    if last_pin_failure is not None:
        # The other half of "why": the target matched several elements and the
        # index could not be measured. Without it the author reads "ambiguous"
        # and never learns what was missing. This string is human-facing only.
        message += f" (nie udało się zmierzyć indeksu: {last_pin_failure.message})"
    return TargetResolutionError(message)


async def resolve_step_target(
    root: Page | Frame,
    step: Step,
    kind: str,
    reasoner: Reasoner,
) -> ResolvedTarget | TargetAbsent:
    """Resolve and validate ``step``'s target against the live ``root``.

    Returns :class:`TargetAbsent` only for the narrow verdicts in
    :data:`ABSENT_REASONS`; every other failure — an ambiguous description, an
    invented ``inputText``, a sensitive field, a target that never validates,
    a ``<select>`` without the wanted option — raises
    :class:`TargetResolutionError`, because those are authoring or resolver bugs
    rather than a missing element. Whatever the injected ``Reasoner`` itself
    raises passes through unchanged and keeps its own type.

    An ambiguous target is no longer an automatic re-prompt: when the reasoner
    named the candidate it meant, the index that pins the target to that exact
    element is *measured* here (:func:`pin_position`) and reported back on
    :attr:`ResolvedTarget.pinned`. Only a failure to measure it becomes feedback
    for another round — and only the wording of that failure, which by
    construction holds nothing but counts and ids this function itself minted,
    ever reaches a prompt.
    """

    instruction = step_instruction(step)
    # `highlight` points at a region, so its candidate set includes containers a
    # clicking command has no use for; every other kind gets today's set.
    candidates = await collect_candidates(root, roles=candidate_roles_for(kind))
    candidate_ids = {candidate.id for candidate in candidates}
    option = step.select.option if step.select is not None else None
    resolution_error: str | None = None
    last_rejection: ValidationFail | None = None
    last_pin_failure: PinFail | None = None
    feedback: str | None = None

    accepts_feedback = _accepts_feedback(reasoner)

    for _ in range(MAX_REPROMPT):
        # Passed only when there is something to say *and* the implementation can
        # hear it — see `_accepts_feedback`. An old two-argument double simply
        # gets asked again without the correction rather than raising.
        result = (
            await reasoner.resolve(instruction, candidates, feedback=feedback)
            if feedback and accepts_feedback
            else await reasoner.resolve(instruction, candidates)
        )
        feedback = None
        if isinstance(result, ReasonerError):
            if result.reason in ABSENT_REASONS:
                return TargetAbsent(reason=result.reason, message=result.message)
            raise TargetResolutionError(f"reasoner: {result.reason}: {result.message}")
        assert isinstance(result, ReasonerResult)

        action = action_for(kind, result.action)
        if result.candidate_id is not None and result.candidate_id not in candidate_ids:
            # Fail-closed. An id we never sent cannot key a pin, and it is model
            # output, so it may carry page text — hence it is neither quoted back
            # to the model nor put in the error. Rejecting it here also buys a
            # guarantee downstream: every id that reaches `pin_position`, and
            # therefore every id inside a `PinFail.message`, is one we minted.
            feedback = _UNKNOWN_CANDIDATE_FEEDBACK.format(count=len(candidates))
            resolution_error = "reasoner wskazał candidateId spoza listy kandydatów"
            continue

        infers_text = action == "type" and kind == "teach"
        input_text = result.input_text if infers_text else None
        # Also the per-round reset of `resolution_error`: a reason recorded by an
        # earlier round must not survive a round that got this far.
        resolution_error = _teach_input_text_error(instruction, input_text) if infers_text else None
        if resolution_error is not None:
            continue

        state = step_state(step)
        # A hidden wait is the one thing never pinned. Its `identity` is `None` by
        # design, so there is nowhere to freeze the DOM path a drift check would
        # need; and `reuse_failure` returns early for `hidden` on `count() <= 1`,
        # which a locator carrying `.nth(n)` always satisfies. A pinned hidden wait
        # would therefore be unremovable — neither `wait_ambiguous` nor drift could
        # ever invalidate it. Ambiguity there keeps today's behaviour: re-prompt.
        may_pin = not (action == "waitFor" and state == "hidden")

        outcome = await _accept_any_variant(
            root, result.target, action, option, result.candidate_id, may_pin
        )
        if isinstance(outcome, _NoVariantAccepted):
            rejection = outcome.rejection
            last_rejection = rejection if rejection is not None else last_rejection
            last_pin_failure = (
                outcome.pin_failure if outcome.pin_failure is not None else last_pin_failure
            )
            feedback = outcome.feedback
            if action == "select" and rejection is not None and rejection.reason == "not_visible":
                # Not a reasoner miss to be re-prompted away silently: the page
                # has no control a viewer could see for this select, so the
                # choice cannot be filmed at all. Re-prompting may still find a
                # *different* select, so the loop continues — but if it does not,
                # the run must say which situation this is rather than "could not
                # validate the target".
                resolution_error = (
                    f"nie znaleziono widocznej kontrolki dla listy {instruction!r} "
                    "— strona ukryła <select> i nic widocznego go nie zastępuje, "
                    "więc nie da się pokazać wyboru na filmie"
                )
            continue
        accepted = outcome

        if infers_text and await is_sensitive_type_target(accepted.validation.locator):
            resolution_error = (
                "pole wygląda na przeznaczone dla wartości wrażliwej; użyj enterText z ENV"
            )
            continue

        # freeze identity BEFORE the action (the DOM may change); waitFor:hidden has none
        identity = (
            None
            if action == "waitFor" and state == "hidden"
            else await capture_identity(accepted.validation.locator)
        )
        return ResolvedTarget(
            action=action,
            target=accepted.target,
            locator=accepted.validation.locator,
            input_text=input_text,
            state=state,
            identity=identity,
            pinned=accepted.pinned,
        )

    raise _exhausted_error(instruction, resolution_error, last_rejection, last_pin_failure)
