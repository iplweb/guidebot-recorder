"""The compiled-sidecar contract, as render reads it.

What ``compile`` froze and what render is allowed to replay: the fingerprint check
(:func:`_compiled_action_is_current`), in-place resolution of a still-pending entry
against the live page (:func:`_resolve_pending_target`), and the ``CachedAction``
that replaces it on disk (:func:`_freeze_resolved`).

Deliberately free of browser choreography — it decides *whether* a step may run as
frozen, never *how* it runs. :func:`_compiled_from` is a thin alias over the
compiler's own rule rather than a second implementation: render's copy used to be
a verbatim duplicate, so extending one side would have made every sidecar look
stale to the other.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin

from playwright.async_api import Frame, Page

from guidebot_recorder.models.action import (
    COMPILER_VERSION,
    CachedAction,
    Fingerprint,
    PendingAction,
)
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    TargetAbsent,
    compiled_from,
    resolve_step_target,
    step_instruction,
)

from .errors import _OptionalAbsent

#: how often a pending gate is re-resolved while its wait window is still open
_PENDING_POLL_SECONDS = 0.25


def _resolve_url(scenario: Scenario, url: str) -> str:
    base = scenario.config.base_url
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


def _compiled_from(step: Step) -> str:
    """What ``compile`` froze this step's fingerprint against.

    A thin alias, deliberately not a second implementation: render's copy of
    this rule used to be a verbatim duplicate of the compiler's, so extending
    one side (with the per-step ``select.mode``) would have made every sidecar
    look stale to the other.
    """

    try:
        return compiled_from(step)
    except ValueError as exc:
        raise ValueError(f"krok {step.command_kind()} nie wymaga cachedAction") from exc


def _compiled_action_is_current(
    step: Step, action: CompiledAction | None, scenario_hash: str
) -> bool:
    """Check source/config fingerprints before replaying frozen behavior."""

    if not step.requires_target():
        return action is None
    if action is None:
        return False
    kind = step.command_kind()
    expected_state = step.wait.state if isinstance(step.wait, WaitUntil) else None
    if isinstance(action, PendingAction):
        # Nothing was frozen yet, so there is no action/expect to cross-check —
        # only that the placeholder still stands for *this* step and config.
        fingerprint = action.fingerprint
        return (
            fingerprint.compiler_version == COMPILER_VERSION
            and fingerprint.command_kind == kind
            and fingerprint.compiled_from == _compiled_from(step)
            and fingerprint.config_hash == scenario_hash
            and fingerprint.state == expected_state
        )
    expected_action = {
        "click": "click",
        "hover": "hover",
        "enterText": "type",
        "select": "select",
        "highlight": "highlight",
        "wait": "waitFor",
    }.get(kind)
    if expected_action is not None and action.action != expected_action:
        return False
    fingerprint = action.fingerprint
    if not (
        fingerprint.compiler_version == COMPILER_VERSION
        and fingerprint.command_kind == kind
        and fingerprint.compiled_from == _compiled_from(step)
        and fingerprint.config_hash == scenario_hash
        and fingerprint.state == expected_state
        and fingerprint.expect == action.expect
    ):
        return False
    return not (
        kind == "teach"
        and action.action == "type"
        and (action.input_text is None or action.input_text not in step.teach)
    )


async def _resolve_pending_target(
    root: Page | Frame,
    step: Step,
    kind: str,
    reasoner: Reasoner,
) -> ResolvedTarget:
    """Resolve a :class:`PendingAction` against the live page, polling while it may still appear.

    A gate (`wait: {until: ...}`) gets the whole configured wait window, retried on
    an interval: the canonical gating element — a cookie banner — is injected after
    a delay, and a single snapshot would report a spurious absence and silently
    delete the branch from the video. Any other optional step has no wait window,
    so it is resolved exactly once.

    Raises :class:`~guidebot_recorder.recorder.render.errors._OptionalAbsent` when
    the window closes on an "absent" verdict; every other resolver failure
    propagates out of ``resolve_step_target``.
    """

    window = step.wait.timeout if isinstance(step.wait, WaitUntil) else 0.0
    deadline = time.monotonic() + window
    while True:
        result = await resolve_step_target(root, step, kind, reasoner)
        if isinstance(result, ResolvedTarget):
            return result
        assert isinstance(result, TargetAbsent)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _OptionalAbsent(f"{step_instruction(step)!r}: {result.reason} ({result.message})")
        await asyncio.sleep(min(_PENDING_POLL_SECONDS, remaining))


def _freeze_resolved(
    step: Step,
    kind: str,
    resolved: ResolvedTarget,
    expect: str,
    scenario_hash: str,
) -> CachedAction:
    """Build the ``CachedAction`` that replaces a pending entry in the sidecar.

    ``opens_popup`` stays false by construction: a click resolved at render time
    carries no popup observation from compile, and the render popup contract is
    what fails loudly if one opens anyway (a documented limitation of branches).
    """

    return CachedAction(
        action=resolved.action,
        target=resolved.target,
        identity=resolved.identity,
        expect=expect,
        state=resolved.state,
        input_text=resolved.input_text,
        fingerprint=Fingerprint(
            command_kind=kind,
            compiled_from=_compiled_from(step),
            expect=expect,
            config_hash=scenario_hash,
            state=resolved.state,
        ),
    )
