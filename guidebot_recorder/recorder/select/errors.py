"""Every way a ``select:`` step can refuse, and the exact words for each.

One module holds the exception type, the two ``reason`` values and all nine
message builders, because the thing that must not drift is *which refusal this
is* — and that verdict is spread over three call sites for some of them. A
disabled option is discovered both by reading the ``<select>`` and by reading
the row the shim rendered; "no such option" is established on four different
paths. Each of those pairs must produce the same wording and the same
``reason``, and a message written where it is raised cannot promise that.

**The builders are synchronous and take strings.** They ask the page nothing:
the caller has already described the control
(:func:`~guidebot_recorder.recorder.select.probe.describe`) and already read
whatever state the wording needs. That is deliberate rather than incidental —
:func:`~guidebot_recorder.recorder.select.probe.require_option` raises these,
so if the builders in turn called the probe the two modules would import each
other. Keeping this module free of Playwright, of the page-side JS and of
``await`` is what makes the dependency one-way, and it has the side effect that
every message here is testable without a browser.

**Timeouts arrive as arguments, never as imports.** Two messages quote a limit
that also governs a wait — :data:`~...select.driver.OPTION_WAIT_MS` and
:data:`~...select.probe.READY_WAIT_MS` — and the tests shrink both to keep the
suite fast. ``from … import OPTION_WAIT_MS`` here would bind the value at
import time, so a patched run would wait 400 ms and then announce a limit of
5000 ms. ``limit_ms`` is a parameter so the number in the sentence is by
construction the number that was actually waited.
"""

from __future__ import annotations

from guidebot_recorder.selects.selects import SelectsNotReadyError

#: :attr:`SelectDriveError.reason` — the control does not offer that option.
#:
#: The one cause a caller may legitimately answer with something other than
#: "fail": an ``optional: true`` step means "do this if it is on offer", and a
#: dropdown that no longer lists the label is exactly the absence that clause
#: describes. It is the same verdict ``validate.reuse_failure`` reports as
#: ``option_missing`` for a step it *does* get to preflight — an optional step
#: skips that preflight, so the miss can only surface from the drive itself.
OPTION_MISSING = "option_missing"

#: :attr:`SelectDriveError.reason` — anything else, and therefore a failure.
#:
#: Deliberately the default, so a raise site that says nothing is loud: a click
#: that did not take, a widget with nothing to unfurl, a shim taken off the
#: select mid-step and a row the page never rendered are all *broken steps*,
#: and a caller that quietly skipped them would hide the very failures this
#: choreography exists to surface.
UNDRIVABLE = "undrivable"


class SelectDriveError(RuntimeError):
    """A ``select:`` step could not be performed *visibly*.

    Raised when the choreography has nothing for the cursor to click: the page
    enhanced the ``<select>`` itself and the association heuristic found no
    visible control, the select is on screen but has no DOM option list to
    unfurl, or the list that did unfurl never produced a row matching the option
    label within the option wait. The message names which of those it is,
    because the three want different fixes.

    There is deliberately no fallback to ``select_option()``. Falling back would
    restore exactly the invisible value change this choreography exists to
    remove, and would do it unobservably — the run would succeed and only a
    viewer would ever discover the step is unwatchable.

    ``reason`` splits "this control does not offer that option"
    (:data:`OPTION_MISSING`) from every other way the step can fail
    (:data:`UNDRIVABLE`). The split exists because those two answer the
    ``optional: true`` question differently and nothing else does: a label that
    is not on the list is the absence an optional step is allowed to shrug off,
    while a click that did not take, an undrivable widget, a shim removed
    mid-step and a row that never appeared all mean the step is broken however
    the author marked it. It is a machine-readable field rather than a message
    substring for the obvious reason — the messages are Polish prose written to
    be rewritten.

    The render layer catches this and re-raises it as a ``RenderError`` carrying
    the index of the failing step. ``RenderError`` lives in
    ``recorder/render/errors.py``, whose package imports the recorder, so it
    cannot be raised from here without a cycle.
    """

    def __init__(self, message: str, *, reason: str = UNDRIVABLE) -> None:
        super().__init__(message)
        self.reason = reason


def no_control_error(described: str, option: str, state: dict) -> SelectDriveError:
    """Name the situation the select is actually in, not just the empty result.

    Three different pages end up here and they need three different fixes,
    so the message must not collapse them:

    * the page hid the ``<select>`` and the association heuristic found
      nothing standing in for it — a widget library that failed to
      initialise, or one whose control loads over the network;
    * the select is on screen and there is no shim layer in this context at
      all, which is ``config.selects.mode: native`` (or a bare context such
      as a health probe). Nothing is going to unfurl, ever;
    * the select is on screen, the shim ran, and it declined this one —
      a marker class it honours, or ``mode: native`` pinned onto it.

    The middle case used to be unreachable: the caller sent it into the
    association heuristic, which always finds *something*, so the run
    clicked an unrelated sibling on camera and then blamed the option.

    ``state`` is a :data:`~guidebot_recorder.recorder.select._js._SHIM_STATE_JS`
    read the caller already has — this asks the page nothing of its own.
    """

    # Medium-neutral wording: the PDF guide raises these too, and a message
    # about "the film" reaches an author who asked for a document.
    tail = f'nie da się pokazać rozwiniętej listy z opcją „{option}"'
    if state["hidden"]:
        return SelectDriveError(
            f"strona ukryła {described} i nie znaleziono widocznej kontrolki, "
            f"która ją zastępuje — {tail}"
        )
    if not state["installed"]:
        return SelectDriveError(
            f"{described} jest widoczna, ale w tym kontekście nie zainstalowano "
            f"nakładki select, więc nie ma czego rozwinąć — {tail}. Najczęstsza "
            f"przyczyna to `config.selects.mode: native` (wtedy skrypt nakładki "
            f"w ogóle nie jest wstrzykiwany) — użyj `config.selects.mode: shim` "
            f"albo `mode: native` na tym kroku"
        )
    marker = state["markerClass"]
    cause = (
        f"nakładka pominęła ją z powodu klasy `{marker}`" if marker else "nakładka jej nie objęła"
    )
    return SelectDriveError(
        f"{described} jest widoczna, ale nie ma listy opcji w DOM — {cause}, "
        f"a listę natywnego selecta rysuje system operacyjny; {tail} "
        f"(użyj `mode: native`, jeśli sam wybór wystarczy)"
    )


def option_missing_error(described: str, option: str) -> SelectDriveError:
    """This ``<select>`` does not carry that label — the one skippable cause.

    The only refusal a caller is allowed to answer with anything other than
    "fail" (see :data:`OPTION_MISSING`), so it is built in one place and
    every path that can establish the fact routes through here — including
    the two direct ones, which would otherwise leave the miss to
    ``select_option``'s actionability timeout and report it as a Playwright
    error nobody can classify.
    """

    return SelectDriveError(
        f'lista {described} nie zawiera opcji „{option}"', reason=OPTION_MISSING
    )


def no_option_error(described: str, option: str, *, limit_ms: float) -> SelectDriveError:
    """The list unfurled but the row never turned up — cause unestablished.

    Deliberately *not* :data:`OPTION_MISSING`. On the shimmed path this is
    only reached with the option's index already in hand, so the label is
    demonstrably there and it is the rendering that failed; on the page's own
    widget the caller has already checked the underlying ``<select>``, so a
    row that still does not appear means the widget did not draw it. Either
    way the step is broken, and a caller must not shrug it off as "the option
    was not on offer".

    ``limit_ms`` is the wait that just elapsed, handed in rather than read from
    a module global — the tests shrink that wait, and a message quoting the
    unpatched constant would be a lie that no assertion catches.
    """

    return SelectDriveError(
        f'po rozwinięciu {described} nie pojawiła się opcja „{option}" (limit {limit_ms} ms)'
    )


def disabled_option_error(described: str, option: str) -> SelectDriveError:
    """A ``disabled`` option refuses every path this recorder can drive it by.

    Built in one place and raised from two observations of the same fact:
    :func:`~guidebot_recorder.recorder.select.probe.require_option` reading the
    ``<select>``, and ``SelectDriver._shim_option_row`` reading the row the shim
    rendered after beat 1 (where the ``<select>`` is no longer the whole truth —
    the shim may have been taken off it mid-step). One wording, so a disabled
    option reads the same regardless of which choreography found it.

    :data:`UNDRIVABLE`, emphatically not :data:`OPTION_MISSING`: the option
    *is* there, on offer and spelled correctly — the page simply refuses it.
    An ``optional:`` step means "do this if it is offered", so it must fail
    here rather than shrug, or a scenario would silently stop exercising a
    control the page deliberately locked.
    """

    return SelectDriveError(
        f'opcja „{option}" na liście {described} jest '
        f"wyłączona (`disabled`) — nie da się jej wybrać ani z rozwiniętej "
        f"listy, ani bezpośrednio"
    )


def unshimmed_mid_step_error(described: str, option: str, marker: str | None) -> SelectDriveError:
    """Beat 1 opened our list; by beat 2 the shim was no longer on this select.

    The classification pass runs on every mutation, so a page that enhances
    a select on first interaction (select2 hydrating on ``mousedown``) can
    legitimately take the control over between the two beats: the marker
    class appears, the observer unshims, and the rows the second beat was
    about to click are gone with it.

    Named as its own failure because the fix is not the one every other
    beat-2 message points at. The option label is fine — the recording just
    raced the page's own widget, and the answer is a longer
    ``selects.openHoldMs`` (or ``settleMs``), or ``mode: native`` for that
    step, not a corrected label.

    ``marker`` is the class that caused the reclassification, re-read after
    beat 1 by the caller, or ``None`` when the page changed the control some
    other way.
    """

    because = (
        f"strona przejęła kontrolkę (klasa `{marker}`)" if marker else "strona zmieniła kontrolkę"
    )
    return SelectDriveError(
        f"nakładka nad {described} zniknęła w trakcie kroku "
        f"— {because} już po rozwinięciu listy, więc nie ma czego kliknąć dla "
        f'opcji „{option}". Zwiększ selects.openHoldMs albo selects.settleMs, '
        f"aby strona zdążyła się ulepszyć przed krokiem, albo ustaw dla tego "
        f"kroku mode: native"
    )


def hidden_listbox_error(described: str, option: str) -> SelectDriveError:
    """The listbox itself has no box — nothing stands in for it to click instead.

    Unlike the shimmed and page-widget shapes, a ``multiple`` / ``size > 1``
    select needs no stand-in when it is visible: its ``<option>`` rows are
    already laid out in the page (see ``SelectDriver._select_in_listbox``). That
    is exactly why there is nothing to fall back to when it is *not* visible —
    a native ``<select>`` would be exactly as hidden, so ``mode: native``
    would not help here either. Same reasoning, same omission, as
    :func:`no_control_error`'s ``hidden`` branch.

    :data:`UNDRIVABLE`, and never :data:`OPTION_MISSING`: nothing here says
    anything about which options the control offers — the option list was
    never even reached. An ``optional:`` step must fail on a control an
    earlier step hid or a stale compile left behind, exactly as a mandatory
    one does.
    """

    # Medium-neutral wording, like `no_control_error`'s: the PDF guide
    # reaches this too, and "on the film" would reach an author who asked
    # for a document.
    tail = f'nie da się pokazać wyboru opcji „{option}"'
    return SelectDriveError(
        f"{described} nie ma żadnego rozmiaru na stronie "
        f"(element ukryty albo bez layoutu) — {tail}. Sprawdź, czy "
        f"wcześniejszy krok scenariusza nie ukrył tego pola, albo czy układ "
        f"strony zmienił się od czasu `compile` — jeśli tak, uruchom "
        f"`compile --force`"
    )


def not_selected_error(described: str, option: str, actual: str | None) -> SelectDriveError:
    """The click landed and the ``<select>`` is still showing something else.

    The verdict of the read-back every on-camera path ends with
    (:func:`~guidebot_recorder.recorder.select.probe.confirm_selected`). A click
    is not evidence: a disabled row refuses it, a page widget can hand back a
    decoy node carrying the same label, and a page can cancel the event
    outright. In all three the value never changes and there is no exception
    anywhere to notice — so the message names both suspects rather than
    guessing between them.
    """

    shown = f'„{actual}"' if actual is not None else "nic"
    return SelectDriveError(
        f'kliknięcie opcji „{option}" w {described} nie zmieniło wyboru — '
        f"wybrane jest {shown}. Opcja mogła być wyłączona (`disabled`), "
        f"albo kursor trafił w element, który tylko powtarza tę etykietę"
    )


def snapshot_lost_error(described: str, option: str) -> SelectDriveError:
    """Beat 1 replaced the document, so "appeared after the click" means nothing.

    The page-widget path recognises option rows only by the snapshot taken
    before beat 1, so a missing snapshot is not a degraded search — it is no
    search at all, and must be said out loud rather than answered with the
    first node on the page that happens to carry the label.
    """

    return SelectDriveError(
        f"stan sprzed rozwinięcia listy {described} zniknął "
        f"— dokument został podmieniony w trakcie kroku, więc nie da się odróżnić "
        f"świeżo narysowanych opcji od reszty strony; nie da się pokazać "
        f'rozwiniętej listy z opcją „{option}"'
    )


def not_ready_error(url: str, *, limit_ms: float) -> SelectsNotReadyError:
    """Phrased like ``Selects._not_ready``: same failure, same two fixes.

    The odd one out here — it carries ``SelectsNotReadyError``, not
    :class:`SelectDriveError` — but it belongs with the others because it is a
    message, and the whole point of this module is that the wording for one
    failure exists once. ``limit_ms`` is the bound that actually elapsed; see
    the module docstring for why it is not imported.
    """

    return SelectsNotReadyError(
        f"widget select nie zgłosił gotowości w ciągu {limit_ms / 1000:.1f} s "
        f"dla ramki {url or '(nieznany adres)'}. "
        f"Zwiększ selects.settleMs, jeśli strona długo się inicjalizuje, albo "
        f"ustaw selects.mode: native, aby zrezygnować z podmiany list "
        f"rozwijanych na tej stronie."
    )
