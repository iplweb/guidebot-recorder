"""Guard: every ``video.mux`` test seam must still reach the consumer it patches.

Splitting ``mux.py`` into a package moved every consumer into a module of its own.
A consumer that writes ``from .ffmpeg import _run`` binds the function object at
import time, so ``monkeypatch.setattr(mux_module.ffmpeg, "_run", fake)`` rebinds a
name nobody reads any more. The patch reaches nobody and the test keeps passing
while checking nothing — ``detect_content_crop`` degrades to ``None`` on *any*
failure, so its assertion still holds and the run quietly shells out to real
ffmpeg. The only defence is late binding: reach a seam through the module object.

The invariant, the discovery scan and every check live in :mod:`tests._seam_guard`,
which both this guard and ``tests/unit/recorder/test_compile_seams.py`` drive. Read
that module first; this file only says what is specific to ``video.mux``.

**What is specific to this package: all three seams are defined inside it.**
``_run`` and ``_run_to_output`` live in ``mux/ffmpeg.py``, ``probe_duration`` in
``mux/probe.py``. The coupled pair (see the helper's docstring) therefore resolves
to its second row for every one of them — consumers are rewired to ``mod.name(...)``
and the tests patch the *defining* module — inside the package and out
(``video/timeline.py``, ``video/sfx.py``, ``video/audiobed.py``,
``recorder/render/``), so one patch line covers every consumer of a name.
``SeamGuard.assert_patch_targets`` derives that mode from the definition site
rather than assuming it, which is why the sibling package can hold the opposite
rule without a second implementation.

**There is no allowlist, and zero is the right number of entries.** A "harmless"
re-export is a laundering channel: a module that name-imports a seam becomes
itself an exporter of an early-bound copy, and every consumer that goes through
the re-export is invisible to a patch on the defining module *and* to a scan that
filters on the substring "mux". The one entry this file used to carry
(``guidebot_recorder.video`` re-exporting ``probe_duration``) bought exactly that
hole and had no callers; it is gone.

**The seam list is discovered, not written down here.** It is read out of
``tests/`` by finding what is actually patched on the mux package, so a seam added
tomorrow is covered the day it appears — a hardcoded list would rot exactly when
it starts mattering. Needs no ffmpeg: this only parses source.
"""

from __future__ import annotations

from tests._seam_guard import SeamGuard

PACKAGE_NAME = "guidebot_recorder.video.mux"

GUARD = SeamGuard.build(PACKAGE_NAME)


def test_the_scan_is_complete() -> None:
    # Non-emptiness is not liveness. One recognised idiom in one file would make
    # `assert SEAMS` pass while every other patch site went unscanned, and every
    # assertion below would be vacuously true — the failure this guard exists to
    # catch. So an unclassifiable target that mentions the package is an error.
    GUARD.assert_scan_complete()


def test_every_patch_targets_a_submodule_not_the_facade() -> None:
    # Patching the facade cannot work: the consumer lives in a submodule and reads
    # that submodule's globals. For this package the right submodule is the one
    # that *defines* the seam, because that is the mode all three seams are in.
    GUARD.assert_patch_targets()


def test_facade_withholds_every_patched_name() -> None:
    # Re-exporting a seam would let a patch on the facade succeed and reach
    # nobody. Withholding turns the same mistake into an AttributeError.
    GUARD.assert_facade_withholds()


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into another module's globals, all
    # equally fatal and all invisible to a patch on the defining module:
    # `from X import seam`, `alias = X.seam`, and `def f(..., _seam=X.seam)`.
    GUARD.assert_no_early_binding()


def test_multi_consumer_seams_are_patched_on_every_consumer() -> None:
    # Dormant here by construction — an inside-defined seam has one home and may
    # not be name-imported at all, so it cannot grow a second consumer without
    # first failing the test above. It is still asserted rather than assumed: the
    # day a seam moves out of the package this is the check that notices.
    GUARD.assert_multi_consumer_coverage()
