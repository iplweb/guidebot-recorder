"""Disposing of asyncio work the render no longer needs.

Every bounded probe in this package — the two ``window.open`` lookups, the popup
content-box measurement — can outlive the answer it was started for. They share
one disposal rule rather than each inventing its own, which is why it lives here
and not next to any one of them.
"""

from __future__ import annotations

import asyncio


def _discard_pending(task: asyncio.Future) -> None:
    """Cancel a lookup we no longer need, and never orphan its exception.

    A cancelled ``Frame.evaluate`` can still settle later — typically with
    ``Frame was detached`` — and an unread exception on an abandoned future is
    what produces asyncio's "Future exception was never retrieved" noise.
    Retrieving it in a done-callback keeps that silent.
    """

    if task.done():
        # Already settled, so there is nothing to cancel — but a probe that
        # raised and was never read is orphaned just the same. Read it.
        if not task.cancelled():
            task.exception()
        return
    task.cancel()
    task.add_done_callback(lambda done: done.cancelled() or done.exception())
