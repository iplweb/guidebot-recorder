"""The cursor trail: where the previous guide page left the reader's eye.

One class, its own module, because the invariant it enforces used to be a
comment. See :class:`_CursorTrail`.
"""

from __future__ import annotations

from guidebot_recorder.guide.geometry import Shape

_Point = tuple[float, float]


class _CursorTrail:
    """Where the previous page left the reader's eye, and the shape it left it on.

    The pair is deliberately private and reachable only through :meth:`advance`,
    which reads the outgoing values and adopts the incoming ones **as a single
    expression**. That is the whole design. As two statements — the shape of this
    code before — the write had to sit *after* the page was built, because
    ``annotations_for`` reads the previous target's shape as the arrow's origin,
    and nothing but a comment stopped a later edit from moving one line up. The
    result would be an arrow that starts on the very target it points at: a wrong
    picture in a PDF that still renders. Called from an argument position there is
    no statement boundary left to slip the overwrite into.

    :meth:`reset` drops both halves together for the same reason: a navigate and
    a scroll each cleared two locals on consecutive lines, and those can drift.
    """

    def __init__(self) -> None:
        self._cursor: _Point | None = None
        self._shape: Shape | None = None

    def advance(self, *, cursor: _Point | None, shape: Shape | None) -> dict:
        """The pair the page being built points *from*, replaced by its own.

        Returns the ``prev_cursor``/``prev_shape`` keywords ``annotations_for``
        expects, so the call site reads ``**trail.advance(...)`` and cannot get
        the two the wrong way round either.
        """

        previous = {"prev_cursor": self._cursor, "prev_shape": self._shape}
        self._cursor, self._shape = cursor, shape
        return previous

    def reset(self) -> None:
        """Forget both halves: the page they described is no longer on screen."""

        self._cursor = None
        self._shape = None
