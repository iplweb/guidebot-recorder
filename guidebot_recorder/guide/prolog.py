"""Static (no-browser) checks and page classification for the PDF guide."""

from __future__ import annotations

from typing import Literal

from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import FlatStep


class GuideError(Exception):
    """A scenario the guide cannot render (unresolved mandatory step, broken sidecar)."""


PageKind = Literal["gate", "navigate", "slide", "action", "scroll", "text", "wait", "closeWindow"]

#: Commands the guide replays in the browser off a frozen target. `highlight` is
#: one of them even though it performs nothing: the guide still has to resolve the
#: target to know where to draw its ellipse.
ACTION_KINDS = frozenset({"click", "hover", "enterText", "teach", "select", "highlight"})
#: Commands with no browser work in a still-image pass: a film-only flourish
#: (`desktop`) and a bare `say`. All that survives into the PDF is their narration.
#:
#: `closeWindow` used to sit here, back when a popup was rejected outright and the
#: command therefore had nothing to close. It now drives the window switch back to
#: the main page, so it is browser work and has a page kind of its own.
NARRATION_ONLY_KINDS = frozenset({"desktop", "say"})
#: Every command `classify` knows how to place. A command outside this set is a
#: gap in the guide, not a no-op — see :func:`scan_for_blockers`.
SUPPORTED_KINDS = (
    ACTION_KINDS | NARRATION_ONLY_KINDS | {"navigate", "slide", "scroll", "wait", "closeWindow"}
)


def classify(flat_step: FlatStep) -> PageKind:
    if flat_step.is_gate:
        return "gate"
    step = flat_step.step
    kind = step.command_kind()
    if kind == "navigate":
        return "navigate"
    if kind == "slide":
        return "slide"
    if kind == "scroll":
        return "scroll"
    if kind == "closeWindow":
        return "closeWindow"
    if kind in ACTION_KINDS:
        return "action"
    if kind == "wait":
        return "text" if step.say else "wait"
    # narration-only (`scan_for_blockers` has already rejected anything else)
    return "text" if step.say else "wait"


def scan_for_blockers(flat: list[FlatStep], actions: list) -> None:
    """Raise GuideError for unsupported commands or a mandatory unresolved step."""

    for flat_step, action in zip(flat, actions, strict=True):
        kind = flat_step.step.command_kind()
        if kind not in SUPPORTED_KINDS:
            # Fail here rather than let `classify` file the step under
            # narration-only. That silent fallback is exactly how the missing
            # `select` branch stayed hidden: the step became a text page, the
            # browser never saw the action, and the failure surfaced steps later
            # as an identity mismatch against a page the compiler never saw.
            raise GuideError(f"komenda `{kind}` nie jest obsługiwana w `guide`")
        pending = action is not None and not isinstance(action, CachedAction)
        mandatory = (
            flat_step.branch is None
            and not flat_step.is_gate
            and not flat_step.step.optional
            and flat_step.step.requires_target()
        )
        if pending and mandatory:
            raise GuideError(
                "skompilowany scenariusz ma nierozwiązany krok obowiązkowy — "
                "uruchom `guidebot compile` (lub `compile --force`)"
            )
