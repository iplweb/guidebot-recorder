"""Questions put to a ``<select>``, and the two writes that need no choreography.

Everything here takes a locator (or a frame) and gives an answer back. Nothing
here moves a cursor, opens a list or holds state between calls, which is the
whole reason it is not in ``driver.py``: the choreography is an ordering
argument — approach, reveal, click, confirm — and it is much easier to check
that ordering when the individual observations are somewhere else, each
readable on its own.

The split is not merely tidiness. Three of these are the *single* place a
question is asked, and the module boundary is what keeps them single:

* :func:`require_option` — "can this option be chosen?", asked by four
  different paths;
* :func:`shim_state` — "how is this select presented?", which this codebase
  once had four disagreeing answers to;
* :func:`confirm_selected` — "did the click actually take?", the observation
  that turns a quietly wrong recording into a loud failure.

:func:`set_option_directly` and :func:`pin_native` are writes rather than
questions, but they are single, un-choreographed page calls with no cursor
involved, so they read as part of the same layer.

Raises through :mod:`~guidebot_recorder.recorder.select.errors` and imports
nothing from :mod:`~guidebot_recorder.recorder.select.driver` — the dependency
between the two is one-way, and the errors module stays free of the page so it
cannot point back here.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import ElementHandle, Frame, Locator, Page
from playwright.async_api import Error as PlaywrightError

from guidebot_recorder.recorder.select import _js
from guidebot_recorder.recorder.select.errors import (
    disabled_option_error,
    no_control_error,
    not_ready_error,
    not_selected_error,
    option_missing_error,
)
from guidebot_recorder.resolver.widget import associated_control
from guidebot_recorder.selects.selects import READY_TIMEOUT_MARKER, SelectsNotReadyError

#: Backstop bound, in milliseconds, on :func:`await_selects_ready`.
#:
#: Not the primary barrier — compile and render both take the bounded
#: :meth:`guidebot_recorder.selects.Selects.wait_ready`, whose deadline tracks
#: ``settle_ms``, long before a step reaches the recorder. This is the floor
#: under a *direct* caller, which nothing near this module can force through
#: that barrier first, so it is a flat, generous constant rather than something
#: derived from a config the recorder is never handed.
#:
#: A test seam: the suite shrinks it so a wedged-page test costs 0.3 s instead
#: of 15. That is why nothing may ``from … import`` it — a copy in another
#: module's globals would never see the patch, and the only symptom would be a
#: slow suite. :func:`not_ready_error` is handed the value rather than reading
#: it for the same reason.
READY_WAIT_MS = 15_000


async def describe(control: Locator | ElementHandle) -> str:
    """A short control name for an error message (``select#woj``)."""

    return await control.evaluate(_js._DESCRIBE_JS)


async def shim_state(locator: Locator) -> dict:
    """How this select is presented — see :data:`~...select._js._SHIM_STATE_JS`.

    Read once per step and passed down rather than re-asked, because the answer
    can change *during* a step: a page that enhances a select on first
    interaction reclassifies it between the two beats, and a branch that
    re-read this halfway through would be choosing its path on one answer and
    its error message on another.
    """

    return await locator.evaluate(_js._SHIM_STATE_JS)


async def await_selects_ready(frame: Page | Frame) -> None:
    """Wait until this frame owes no classification pass — but not forever.

    Both production callers (compile, render) take
    :meth:`guidebot_recorder.selects.Selects.wait_ready` first, so this
    normally finds a barrier that has already settled. It is written as a
    bound anyway because that ordering is an invariant of *those* call
    sites, not of this one: a direct caller on a page whose widget is wedged
    would otherwise get an ``evaluate`` that never returns, which is the one
    outcome the barrier design rules out everywhere else.

    Not merely "the first pass has run": every branch of the choreography reads
    whether *this* select is shimmed, and a select the previous step added is
    still unclassified while the debounced pass it triggered is pending. See
    :data:`~guidebot_recorder.recorder.select._js._SELECTS_READY_JS`.

    Raises:
        SelectsNotReadyError: the widget is in this frame and its barrier
            did not settle within :data:`READY_WAIT_MS`.
    """

    try:
        # The page-side race is the primary guard; the outer wait covers a
        # document that has stopped running timers at all.
        await asyncio.wait_for(
            frame.evaluate(_js._SELECTS_READY_JS, READY_WAIT_MS),
            timeout=READY_WAIT_MS / 1000 + 1.0,
        )
    except TimeoutError as exc:
        raise _not_ready(frame) from exc
    except PlaywrightError as exc:
        if READY_TIMEOUT_MARKER not in str(exc):
            raise
        raise _not_ready(frame) from exc


def _not_ready(frame: Page | Frame) -> SelectsNotReadyError:
    """The readiness diagnosis, with this frame's address and the bound that elapsed."""

    return not_ready_error(getattr(frame, "url", "") or "", limit_ms=READY_WAIT_MS)


async def pin_native(locator: Locator) -> None:
    """Drop the shim from this select and keep it off.

    See :data:`~guidebot_recorder.recorder.select._js._PIN_NATIVE_JS` for why
    this has to happen before the cursor sets off rather than after.
    """

    await locator.evaluate(_js._PIN_NATIVE_JS)


async def require_option(locator: Locator, option: str) -> int:
    """ "Can this option be chosen?", asked of the ``<select>`` — the only place.

    Every path that consults the real control before committing goes through
    here: compile's direct set, ``mode: native``, the natively-visible
    listbox and the page-widget beat 2. There is exactly one guard because
    there are exactly two ways the answer can be "no", they are asked in the
    same breath, and a second copy of either could drift from this one — the
    mistake this module already paid for when four definitions of "is this
    select enhanced?" disagreed.

    The two refusals are deliberately *not* interchangeable:

    * no such option — :func:`~...select.errors.option_missing_error`, the one
      refusal an ``optional:`` step may answer with a skip
      (:data:`~...select.errors.OPTION_MISSING`);
    * the option exists but is ``disabled`` —
      :func:`~...select.errors.disabled_option_error`, which is
      :data:`~...select.errors.UNDRIVABLE`, because the option *is* on offer
      and the step asking for it is simply broken.

    Both matter before ``Locator.select_option`` as much as before a glide.
    Playwright raises nothing of its own for either: for a disabled option it
    sits in "waiting for element to be visible and enabled" until the whole
    step timeout elapses, and surfaces a raw English ``TimeoutError`` naming
    neither the control nor the option — well after this function could have
    said both, in Polish, and with a ``reason`` a caller can act on.

    Returns the option's index, which the listbox path needs to address the row;
    :data:`~...select._js._OPTION_INDEX_JS` (via
    :data:`~...select._js._OPTION_STATE_JS`) applies the same
    ``HTMLOptionElement.label`` rule Playwright's ``select_option(label=…)``
    matches on, so this, the row it addresses and the call it guards all agree
    about which labels exist.
    """

    state = await locator.evaluate(_js._OPTION_STATE_JS, option)
    if state["index"] < 0:
        raise option_missing_error(await describe(locator), option)
    if state["disabled"]:
        raise disabled_option_error(await describe(locator), option)
    return state["index"]


async def set_option_directly(locator: Locator, option: str) -> None:
    """Set the value with no list involved, for a control that may be hidden.

    A select the page enhanced itself is routinely ``display: none`` (Tom
    Select), which Playwright's actionability check would sit out until it
    times out. Skipping the check for exactly those is what makes spec §6's
    validation relaxation usable: without it a hidden select would validate
    and then fail to compile.
    """

    visible = await locator.is_visible()
    await locator.select_option(label=option, force=not visible)


async def confirm_selected(locator: Locator, option: str) -> None:
    """Read the select back after the click, and fail if it did not take.

    The whole branch exists so a run that would produce an unwatchable video
    fails loudly rather than succeeding quietly. Every on-camera path ends
    at ``row.click()``, and a click is not evidence: a disabled row refuses
    it, a page widget can hand back a decoy node carrying the same label, and
    a page can cancel the event outright. In all three the value never
    changes and there is no exception anywhere to notice — unlike compile's
    direct path, where ``select_option`` throws.

    So the last thing every path does is ask the ``<select>`` what it is
    actually showing. That is the only observation that means the step is
    watchable.
    """

    # ``" ".join(split())`` is the Python spelling of the page's own
    # ``replace(/\s+/g, " ").trim()``, so the two sides of the comparison
    # are normalised by the same rule.
    actual = await locator.evaluate(_js._SELECTED_LABEL_JS)
    if actual == " ".join(option.split()):
        return
    raise not_selected_error(await describe(locator), option, actual)


async def probe_drivable(locator: Locator, option: str) -> None:
    """Fail at compile time for a widget the render could never drive.

    Only meaningful when the shim actually ran and declined to shim this
    select *because the page took it over* — that is what "the page enhanced
    it itself" looks like from here. With no shim installed there is nothing
    to conclude, and a shimmed select is drivable by construction.

    "Not shimmed" is not on its own evidence of a page widget, though: the
    shim also declines a ``multiple`` / ``size > 1`` select, which needs no
    stand-in because its option list is laid out in the page already. Reading
    that as "the page must have enhanced this" is what used to send a
    perfectly filmable listbox into the association heuristic and fail the
    compile with a missing-widget error.

    **What this does not catch.** It asks whether
    :func:`~guidebot_recorder.resolver.widget.associated_control` resolves *a
    visible element*, not whether that element is the right one. The
    heuristic's last step is "nearest following sibling with a box"
    (``resolver/widget.py``), so a hidden select whose real widget sits
    elsewhere in the document can be blessed by an unrelated neighbour. Compile
    passes regardless — its value-set goes through ``select_option``, never
    through the widget — and the render is where it shows: the cursor clicks
    that neighbour on camera and beat 2 waits for a row that never appears.
    Deciding the *wrong control* case here would mean opening the widget and
    inspecting what came up, which is the render choreography itself; the
    honest boundary is "nothing to click", not "undrivable".
    """

    state = await shim_state(locator)
    if not state["installed"] or state["shimmed"] or state["listbox"]:
        return
    control = await associated_control(locator)
    if control is None:
        raise no_control_error(await describe(locator), option, state)
    try:
        drivable = await control.is_visible()
    finally:
        # The probe only ever asked a yes/no question, so the handle is
        # released either way (see the ownership note on
        # `associated_control`); compile runs this once per `select:` step.
        await control.dispose()
    if drivable:
        return
    raise no_control_error(await describe(locator), option, state)
