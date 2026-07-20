"""Static (no-browser) checks and page classification for the PDF guide."""

from __future__ import annotations

from typing import Literal

from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import FlatStep


class GuideError(Exception):
    """A scenario the guide cannot render (popup, unresolved mandatory step)."""


PageKind = Literal["gate", "navigate", "slide", "action", "text", "wait"]


def classify(flat_step: FlatStep) -> PageKind:
    if flat_step.is_gate:
        return "gate"
    step = flat_step.step
    kind = step.command_kind()
    if kind == "navigate":
        return "navigate"
    if kind == "slide":
        return "slide"
    if kind in ("click", "hover", "enterText", "teach"):
        return "action"
    if kind == "wait":
        return "text" if step.say else "wait"
    # say-only
    return "text" if step.say else "wait"


def scan_for_blockers(flat: list[FlatStep], actions: list) -> None:
    """Raise GuideError for popups anywhere, or a mandatory unresolved step."""

    for flat_step, action in zip(flat, actions, strict=True):
        if isinstance(action, CachedAction) and action.opens_popup:
            raise GuideError(
                "scenariusze z popupem nie są obsługiwane w `guide` v1 (krok otwiera nowe okno)"
            )
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
