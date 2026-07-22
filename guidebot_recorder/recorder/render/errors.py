"""The two exceptions the render phase raises — and nothing else.

Extracted first and deliberately import-free. :class:`RenderError` is raised from
almost every other module in this package, so leaving it beside ``run_render``
would have made :mod:`~guidebot_recorder.recorder.render._run` a dependency of the
whole package and closed a total import cycle.
"""

from __future__ import annotations


class RenderError(RuntimeError):
    """A step needs (re-)compile: missing action or mismatched identity."""


class _OptionalAbsent(Exception):
    """The element an *optional* step or branch gate stands for is simply not there.

    Deliberately narrow, and deliberately not a :class:`RenderError`: it is raised
    only from the four signals the design admits as "absent" (a timed-out cached
    ``waitFor``, a ``no_action``/``no_handle`` verdict, an elapsed poll window, a
    failed ``reuse_is_valid``). Everything else — an ambiguous description, a click
    that fails on a resolved target, a navigation error — keeps propagating and
    fails the render, so ``optional`` cannot decay into ``except Exception: pass``.
    """
