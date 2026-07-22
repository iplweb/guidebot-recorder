"""Guard: every ``recorder.compile`` test seam must still reach the consumer it patches.

Splitting ``compile.py`` into a package moved every consumer into a module of its
own. The two seams are names the package pulls in from elsewhere and the tests
substitute: ``write_compiled`` (read by the ``checkpoint`` closure inside
``run_compile``) and ``resolve_step_target`` (read by ``_compile_step``). Before
the split both lived in one module's globals and one patch line covered them; now
they live in ``run.py`` and ``step.py``, and a patch aimed at the facade rebinds a
name nobody reads any more.

The invariant, the discovery scan and every check live in :mod:`tests._seam_guard`,
which both this guard and ``tests/unit/video/test_mux_seams.py`` drive. Read that
module first; this file only says what is specific to ``recorder.compile``.

**What is specific to this package: neither seam is defined inside it.**
``write_compiled`` comes from ``scenario.compiled`` and ``resolve_step_target``
from ``resolver.resolution`` — both have consumers of their own that these patches
must not disturb. The coupled pair (see the helper's docstring) therefore resolves
to its *first* row for both: the consumers keep ``from X import name`` and the
tests patch the consuming submodule. The name-import in ``run.py`` and ``step.py``
is not a violation, it **is** the seam. That is the exact opposite of the rule the
mux guard enforces, and the reason the shared helper keys on the definition site
instead of picking one mode. What stays forbidden is a *second* binding: any other
module that pulls the same name in becomes a second copy, and one patch line would
then cover only one of them — see ``SeamGuard.assert_multi_consumer_coverage``,
which turns that from prose into an assertion.

Why that matters more than it looks: both seams sit on paths whose assertions
survive a dead patch. ``test_recompile_reuses_cache_without_rewriting_unchanged_sidecar``
counts calls through a wrapper — a patch that reaches nobody leaves the counter at
zero and the test would have to be read closely to notice it is now asserting
about nothing. The only defence is to keep the patch and the consumer on the same
module object.

**There is no allowlist, and the only permitted binding is the patch target's
own.** A "harmless" re-export is a laundering channel: a module that name-imports
a seam becomes itself an exporter of an early-bound copy, and every consumer that
goes through the re-export is invisible to a patch on the module the test aims at.

**The seam list is discovered, not written down here.** It is read out of
``tests/`` by finding what is actually patched on the compile package, so a seam
added tomorrow is covered the day it appears — a hardcoded list would rot exactly
when it starts mattering. Needs no browser: this only parses source.
"""

from __future__ import annotations

from tests._seam_guard import SeamGuard

PACKAGE_NAME = "guidebot_recorder.recorder.compile"

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
    # that *name-imports* the seam, because both seams come from outside.
    GUARD.assert_patch_targets()


def test_facade_withholds_every_patched_name() -> None:
    # Re-exporting a seam would let a patch on the facade succeed and reach
    # nobody. Withholding turns the same mistake into an AttributeError.
    GUARD.assert_facade_withholds()


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into the wrong module's globals, all
    # equally fatal and all invisible to the patch the tests actually write:
    # `from X import seam`, `alias = X.seam`, and `def f(..., _seam=X.seam)`.
    GUARD.assert_no_early_binding()


def test_multi_consumer_seams_are_patched_on_every_consumer() -> None:
    # Live here, and the reason this check exists: both seams are name-imported,
    # so either could grow a second consumer in a later split. One patch line
    # would then cover one copy and the other interception path would disappear
    # with no test turning red. Today each has exactly one consumer.
    GUARD.assert_multi_consumer_coverage()
