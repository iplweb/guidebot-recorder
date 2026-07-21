"""Positional index arithmetic — the machine counts, the model does not.

`compile` builds a locator without ``nth`` and, when it matches more than one
element, measures *which one in turn* the model meant by matching DOM paths to
the candidate the model named. The index is therefore **measured, never
guessed** — the exact failure mode issue #51 documents (a model doing array
arithmetic on a JSON snapshot Playwright never counts the same way).

Feedback-message safety contract
--------------------------------
:class:`PinFail.message` in the form that reaches the model's prompt MUST carry
**only numbers and candidate identifiers** (``candidate-<hex>``) — zero text
taken from the page (no control names, nothing describing "how the candidates
differ"). The prompt's whole trust model rests on page-derived text living
exclusively between its ``BEGIN_UNTRUSTED_PAGE_CANDIDATES_JSON`` fences; a
feedback string splicing a page label in would route around it. The messages
here are written to that rule: they interpolate match counts and the caller's
own ``candidate_id`` token, never anything read off an element.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Frame, Page

from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.target import RoleTarget, Target
from guidebot_recorder.resolver.page_context import candidate_ids_of
from guidebot_recorder.resolver.validate import build_locator


@dataclass(frozen=True, slots=True)
class Pinned:
    """A target whose index was measured, not guessed."""

    target: Target
    matches: int  #: how many elements the target matches without ``nth``
    index: int | None  #: ``None`` when there was a single match


@dataclass(frozen=True, slots=True)
class PinFail:
    """Why a target could not be pinned to a single element.

    ``message`` is the template routed to the model — see the module docstring's
    safety contract: numbers and ``candidate-<hex>`` identifiers only, never text
    read off the page.
    """

    reason: Literal[
        "not_found",
        "not_pinnable",
        "no_candidate_id",
        "candidate_not_matched",
        "ambiguous_candidate_id",
    ]
    message: str


async def pin_position(
    root: Page | Frame, target: Target, candidate_id: str | None
) -> Pinned | PinFail:
    """Measure the ``nth`` index that pins ``target`` to the element the model meant.

    ``candidate_id`` is **only ever compared** against the digests computed from
    the live DOM paths (:func:`candidate_ids_of`); it is **never** placed into a
    selector. The proof it cannot leak into one is structural: :func:`build_locator`
    assembles a locator exclusively from the typed structural fields of ``Target``
    and raises ``TypeError`` for anything else, so a candidate identifier has no
    path into the query — it can only select *among* already-matched elements.

    The rules follow the spec table exactly:

    * ``target`` is not a :class:`RoleTarget` → ``PinFail("not_pinnable")``.
      ``nth`` exists only on ``RoleTarget``; ``model_copy(update={"nth": …})`` on
      the other classes does **not** validate and would silently set a field
      ``build_locator`` then ignores.
    * 0 matches without ``nth`` → ``PinFail("not_found")``.
    * 1 match → ``Pinned`` for the ``nth``-less target, ``index=None``.
    * ≥ 2 matches and no ``candidate_id`` → ``PinFail("no_candidate_id")``.
    * ≥ 2 matches and ``candidate_id`` matches exactly one → ``Pinned`` with
      ``nth=i``.
    * ≥ 2 matches and ``candidate_id`` matches none → ``PinFail("candidate_not_matched")``.
    * ≥ 2 matches and ``candidate_id`` matches more than one →
      ``PinFail("ambiguous_candidate_id")`` — fail-closed, in case the DOM path
      turned out non-unique despite the uniqueness fix.
    """

    if not isinstance(target, RoleTarget):
        return PinFail("not_pinnable", "target is not a positional (role) target")

    unpinned = target.model_copy(update={"nth": None})
    locator = await build_locator(root, unpinned)
    matches = await locator.count()

    if matches == 0:
        return PinFail("not_found", "target matched 0 elements")
    if matches == 1:
        return Pinned(target=unpinned, matches=1, index=None)

    if candidate_id is None:
        return PinFail(
            "no_candidate_id",
            f"target matched {matches} elements but no candidateId was provided",
        )

    candidate_ids = await candidate_ids_of(locator)
    hits = [index for index, cid in enumerate(candidate_ids) if cid == candidate_id]

    if not hits:
        return PinFail(
            "candidate_not_matched",
            f"candidateId {candidate_id} matched none of {matches} elements",
        )
    if len(hits) > 1:
        return PinFail(
            "ambiguous_candidate_id",
            f"candidateId {candidate_id} matched {len(hits)} of {matches} elements",
        )

    index = hits[0]
    return Pinned(
        target=target.model_copy(update={"nth": index}),
        matches=matches,
        index=index,
    )


async def pinned_drifted(root: Page | Frame, cached: CachedAction) -> bool:
    """Whether the frozen index today points at a different element than at compile.

    Returns ``False`` — "nothing to check" — for a target that is not a
    :class:`RoleTarget`, a target without ``nth``, a ``cached.identity`` of
    ``None`` (a hidden wait), or a sidecar predating this change
    (``dom_path_digest is None``). The silence for old artifacts is deliberate:
    they froze no path, so any verdict beyond "don't know" would be invented.

    Otherwise the single spec-pinned algorithm: build the locator **without**
    ``nth``, read :func:`candidate_ids_of`, and compare the element at index
    ``target.nth`` against ``cached.identity.dom_path_digest``. An index **out of
    range** (the match list shrank below ``nth``) is **drift**, not an exception.
    """

    target = cached.target
    if not isinstance(target, RoleTarget) or target.nth is None:
        return False
    if cached.identity is None or cached.identity.dom_path_digest is None:
        return False

    unpinned = target.model_copy(update={"nth": None})
    locator = await build_locator(root, unpinned)
    candidate_ids = await candidate_ids_of(locator)

    if target.nth >= len(candidate_ids):
        return True
    return candidate_ids[target.nth] != cached.identity.dom_path_digest
