"""Driving an HTML ``<select>`` on camera — the ``select:`` step, end to end.

Four modules, layered strictly one way so no two of them can point at each
other:

``_js``
    The ten page-side functions. Depends on nothing in this package.
``errors``
    The exception type, the two ``reason`` values and every message. Pure
    string formatting — no Playwright, no ``await``, no page.
``probe``
    Stateless questions put to one ``<select>``, plus the two writes that need
    no choreography. Raises through ``errors``.
``driver``
    :class:`~guidebot_recorder.recorder.select.driver.SelectDriver` — the
    beat-by-beat choreography, and the only part that holds state.

This exists as a package because it was 72% of ``Recorder``'s body while no core
recorder method called into it: the dependency was already one-way, only the
file was not. The recorder now keeps two delegators
(:meth:`~guidebot_recorder.recorder.recorder.Recorder.select` and
:meth:`~guidebot_recorder.recorder.recorder.Recorder.diagnose_select`) and hands
the driver five narrow things instead of itself — see
:mod:`guidebot_recorder.recorder.select.driver` for why a back-reference was
rejected.

Not to be confused with :mod:`guidebot_recorder.selects`, its neighbour: that
package is the *page-side* shim (the JavaScript that replaces the OS popup with
a DOM list, and the Python controller that installs it). This one is the
recorder side that drives whatever the page ended up presenting — shim, page
widget or native listbox.

Two names are deliberately **not** re-exported here: ``OPTION_WAIT_MS`` and
``READY_WAIT_MS``. The suite patches both to keep itself fast, and a patch on
this facade would rebind a name no consumer reads — leaving the tests green and
five to fifteen seconds slower, which is the one regression nothing else would
catch. ``tests/unit/recorder/test_recorder_seams.py`` enforces that.
"""

from __future__ import annotations

from guidebot_recorder.recorder.select.driver import RevealHook, SelectDriver, SelectReveal
from guidebot_recorder.recorder.select.errors import (
    OPTION_MISSING,
    UNDRIVABLE,
    SelectDriveError,
)

__all__ = [
    "OPTION_MISSING",
    "UNDRIVABLE",
    "RevealHook",
    "SelectDriveError",
    "SelectDriver",
    "SelectReveal",
]
