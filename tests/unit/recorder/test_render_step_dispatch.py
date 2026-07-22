"""Guard: ``_replay_action`` must handle every action the sidecar can hold.

``_render_step``'s second dispatch — on ``cached.action`` — **has no ``else``**.
An action it does not recognise does nothing and raises nothing: the step is
silently skipped, the film comes out the right length, and no test in the suite
notices. That is a latent bug, it predates the phase-3 decomposition, and the
design document files it in the backlog because fixing it is a *behaviour*
change:

    docs/superpowers/specs/2026-07-22-code-cleanup-design.md, "Backlog":
    "Dyspozytor akcji w `_render_step` nie ma `else` — nieznana akcja sidecara
    nie robi nic i nie zgłasza błędu."

It is unreachable today, and this file says exactly why: ``CachedAction.action``
is typed ``ActionKind``, a ``Literal`` of six values, and ``CachedAction`` is a
pydantic model, so a sidecar naming anything else is rejected at load. The bug is
therefore not "a render can hit this" but "a **seventh** action can be added to
``ActionKind`` and the dispatch will silently ignore it" — a regression that would
first surface as a step that plays as an empty pause in a finished video.

So this guard pins the totality of the dispatch instead of changing what happens
at runtime: every member of ``ActionKind`` must be named in ``_replay_action``,
and nothing else may be. Adding a seventh action now fails here, at collection
speed, with the name of the action that has no handler.

Reads the source with ``ast``; imports nothing but the type. No browser, no
ffmpeg.
"""

from __future__ import annotations

import ast
import inspect
from typing import get_args

from guidebot_recorder.models.action import ActionKind
from guidebot_recorder.recorder.render import _step as step_module

DISPATCH = "_replay_action"


def _dispatch_function() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(step_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == DISPATCH:
            return node
    raise AssertionError(
        f"{DISPATCH!r} is gone from render/_step.py. If the sidecar dispatch was "
        f"reshaped, this guard has to be reshaped with it — do not delete it: it is "
        f"the only thing standing between a seventh ActionKind and a step that "
        f"plays as an empty pause."
    )


def _handled_actions() -> set[str]:
    """The literals ``_replay_action`` compares ``cached.action`` against."""

    handled: set[str] = set()
    for node in ast.walk(_dispatch_function()):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "action"):
            continue
        handled |= {
            comparator.value
            for comparator in node.comparators
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
        }
    return handled


def test_the_scan_found_the_dispatch() -> None:
    # Without this, a rewrite that stopped comparing `cached.action` to string
    # literals would make `_handled_actions()` empty and turn the real assertion
    # below into "the empty set is missing everything" — noisy, but only by luck.
    # An empty scan is a broken guard, not a passing one.
    assert _handled_actions(), (
        f"{DISPATCH!r} no longer compares `cached.action` against string literals, "
        f"so this guard can no longer see which actions it handles"
    )


def test_every_action_kind_has_a_handler() -> None:
    declared = set(get_args(ActionKind))
    handled = _handled_actions()
    assert declared - handled == set(), (
        f"{sorted(declared - handled)} can appear in a compiled sidecar but "
        f"{DISPATCH!r} does not handle it. The dispatch has no `else`, so such a "
        f"step would be silently skipped: the render succeeds, the film is the "
        f"right length, and the action simply never happens"
    )
    assert handled - declared == set(), (
        f"{DISPATCH!r} handles {sorted(handled - declared)}, which `ActionKind` "
        f"does not declare — dead branches in the one dispatch that must stay total"
    )
