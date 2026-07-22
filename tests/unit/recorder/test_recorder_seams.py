"""Guard: every ``recorder.select`` test seam must still reach the code it patches.

``recorder.py`` was 1329 lines, of which the ``select:`` choreography was 72%.
Splitting it into ``recorder/select/`` moved that choreography, the questions it
asks the page and the words it fails with into four modules of their own, and
every patch that used to name one module object now has to name the right one of
them.

The invariant, the discovery scan and every check live in :mod:`tests._seam_guard`,
which this guard shares with ``tests/unit/video/test_mux_seams.py``,
``tests/unit/recorder/test_compile_seams.py`` and
``tests/unit/recorder/test_render_seams.py``. Read that module first; this file
only says what is specific to ``recorder.select``.

**What is specific to this package: every seam is defined inside it, and two of
the three fail silently.** ``OPTION_WAIT_MS`` (``driver``) and ``READY_WAIT_MS``
(``probe``) are patched purely for speed — 400 ms instead of 5 s, 300 ms instead
of 15 s — by tests that then assert on a *message*. A patch that reaches nobody
leaves every one of those assertions true and simply makes the file twenty
seconds slower, which no assertion anywhere can see. That is the whole reason
this guard exists for this package: the third seam, ``require_option``, would at
least fail loudly (its test counts calls), but the two timeouts would not.

**The timeouts are read twice each, and only one of the two readers is a wait.**
Each limit also appears in the sentence the failure is reported with. Under a
single module that was free; across four it is the classic trap — ``from driver
import OPTION_WAIT_MS`` in ``errors`` would bind 5000 at import time, so a
patched run would wait 400 ms and then announce a limit of 5000. The error
builders take ``limit_ms`` as an argument for exactly that reason, and
:meth:`~tests._seam_guard.SeamGuard.assert_no_early_binding` is what stops the
import from coming back.

**The facade must withhold both.** ``recorder/select/__init__.py`` re-exports the
driver, the error type and the two ``reason`` constants — the names six
production and test modules import — and pointedly not the timeouts. A patch on
the facade would succeed, rebind a name no consumer reads, and reach nobody;
withholding turns that into an ``AttributeError``.

Needs no browser and no ffmpeg: this only parses source and reads the package's
runtime attributes.
"""

from __future__ import annotations

from tests._seam_guard import SeamGuard

PACKAGE_NAME = "guidebot_recorder.recorder.select"

GUARD = SeamGuard.build(PACKAGE_NAME)


def test_the_scan_is_complete() -> None:
    # Non-emptiness is not liveness. One recognised idiom in one file would make
    # `assert SEAMS` pass while every other site went unscanned, and each
    # assertion below would be vacuously true — the failure this guard exists to
    # catch. So an unclassifiable target that mentions the package is an error.
    GUARD.assert_scan_complete()


def test_every_patch_targets_a_submodule_not_the_facade() -> None:
    # All three seams are defined *inside* the package, so the patch target is
    # always the defining submodule: `driver` for the option wait, `probe` for
    # the readiness bound and for `require_option`.
    GUARD.assert_patch_targets()


def test_facade_withholds_every_patched_name() -> None:
    # `recorder/select/__init__.py` deliberately re-exports the error type and
    # the two `reason` constants but neither timeout. Re-exporting a timeout
    # would let a patch on the facade succeed and reach nobody — and the only
    # symptom would be a suite that runs five, or fifteen, seconds slower.
    GUARD.assert_facade_withholds()


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into the wrong module's globals:
    # `from X import seam`, `alias = X.seam`, and `def f(..., _seam=X.seam)`.
    # The live risk here is the first one: both timeouts are quoted in an error
    # message built in `errors`, and a name-import there would freeze the number
    # the message announces at its unpatched value.
    GUARD.assert_no_early_binding()


def test_multi_consumer_seams_are_patched_on_every_consumer() -> None:
    # Vacuous today — no seam here comes from outside the package, so none can
    # have two consuming submodules. Kept because the shape is one import away:
    # the moment a seam is pulled in from `selects/` or `resolver/`, one patch
    # line stops covering it and nothing else would say so.
    GUARD.assert_multi_consumer_coverage()
