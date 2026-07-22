"""Which pages the compile session is allowed to have, and when a popup counts.

The session contract is "one main window plus at most one popup, and a popup may
only be opened by a ``click``". Enforcing it takes three things that belong
together: a view of what the context currently holds
(:func:`_new_pages`, :func:`_unexpected_pages`), the bounded observation
window that decides whether a page that just appeared belongs to the click that
was performed (:func:`_wait_for_new_pages`, over the two constants below), and
the running record of what the session has actually seen (:class:`_PageWatch`).

The constants live here rather than beside the compile loop because they *are*
the window: ``_POPUP_DETECTION_SECONDS`` bounds how long a click may take to
produce a popup, and ``_POPUP_QUIESCENCE_SECONDS`` how long a first popup is
watched for a second one, which is how "exactly one popup" gets detected instead
of assumed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from playwright.async_api import BrowserContext, Page
from playwright.async_api import (
    Error as PlaywrightError,
)

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1


def _new_pages(context: BrowserContext, known: tuple[Page, ...]) -> list[Page]:
    return [
        candidate for candidate in context.pages if all(candidate is not page for page in known)
    ]


def _unexpected_pages(
    observed_pages: list[Page], main_page: Page, popup_page: Page | None
) -> list[Page]:
    """Observed pages outside the main + one-popup session contract."""

    return [
        candidate
        for candidate in observed_pages
        if candidate is not main_page and candidate is not popup_page
    ]


async def _wait_for_new_pages(
    context: BrowserContext,
    known: tuple[Page, ...],
    observed: list[Page] | None = None,
    observed_start: int = 0,
    opened_at: Mapping[Page, float] | None = None,
    *,
    started_at: float | None = None,
    timeout: float = _POPUP_DETECTION_SECONDS,
) -> list[Page]:
    """Find pages opened inside the bounded window of the actual click."""

    loop = asyncio.get_running_loop()
    started_at = loop.time() if started_at is None else started_at
    deadline = started_at + timeout
    cutoff = deadline
    first_seen_at: float | None = None
    while True:
        found: list[Page] = []
        candidates = list((observed or [])[observed_start:]) + _new_pages(context, known)
        for candidate in candidates:
            candidate_opened_at = (opened_at or {}).get(candidate, loop.time())
            if started_at <= candidate_opened_at <= deadline and all(
                candidate is not page for page in found
            ):
                found.append(candidate)
        if found:
            if first_seen_at is None:
                first_seen_at = min((opened_at or {}).get(page, loop.time()) for page in found)
                cutoff = max(cutoff, first_seen_at + _POPUP_QUIESCENCE_SECONDS)
            if loop.time() - first_seen_at >= _POPUP_QUIESCENCE_SECONDS:
                return found
        remaining = cutoff - loop.time()
        if remaining <= 0:
            return found
        await asyncio.sleep(min(0.05, remaining))


# ``eq=False`` is load-bearing, not style: :meth:`observe` is registered as a
# context listener and pyee keys its handler table by hash, so the bound method —
# and therefore the watch itself — has to stay hashable. A dataclass with the
# default ``eq=True`` sets ``__hash__`` to ``None`` and the listener could not be
# registered at all.
@dataclass(eq=False)
class _PageWatch:
    """Which pages a compile session has seen, and which one it is driving.

    The three questions the session contract asks are all questions about this
    object: what is open (:attr:`observed`), when did it open (:attr:`opened_at`),
    and which window is the scenario currently acting on (:attr:`active`).

    :meth:`observe` has to run for *every* page the context opens, including ones
    that appear while a step is being resolved — that is how "a popup may only be
    opened by a click" is detected rather than assumed. The timestamps are what
    let a page be attributed to the click that was running, so they are recorded
    here at the moment of observation and never inferred afterwards.
    """

    main: Page
    active: Page
    loop: asyncio.AbstractEventLoop
    observed: list[Page]
    opened_at: dict[Page, float]
    popup: Page | None = None
    #: Whether a popup was adopted *at any point* in the session, not whether one
    #: is open now — the v1 contract is one popup per session, not one at a time.
    popup_seen: bool = False

    @classmethod
    def starting_at(cls, main: Page) -> _PageWatch:
        """A watch on a session that has only its main window so far."""

        loop = asyncio.get_running_loop()
        return cls(
            main=main,
            active=main,
            loop=loop,
            observed=[main],
            opened_at={main: loop.time()},
        )

    def observe(self, candidate: Page) -> None:
        """Record a page the context just opened. Registered as the listener."""

        if all(candidate is not observed for observed in self.observed):
            self.observed.append(candidate)
            self.opened_at[candidate] = self.loop.time()

    def unexpected(self) -> list[Page]:
        """Observed pages outside the main + one-popup session contract."""

        return _unexpected_pages(self.observed, self.main, self.popup)

    def since(self, mark: int) -> list[Page]:
        """Pages observed after ``mark`` was taken from :func:`len` of the log."""

        return self.observed[mark:]


async def _prepare_popup(page: Page, viewport: dict[str, int]) -> bool:
    """Apply page policy; return false only when the page closed mid-prepare."""

    if page.is_closed():
        return False
    try:
        await page.set_viewport_size(viewport)
        await page.bring_to_front()
        await page.wait_for_load_state()
    except PlaywrightError:
        if page.is_closed():
            return False
        raise
    return not page.is_closed()
