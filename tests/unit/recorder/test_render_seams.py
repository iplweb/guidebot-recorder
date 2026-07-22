"""Guard: every ``recorder.render`` test seam must still reach the consumer it patches.

Splitting ``render.py`` (3016 lines) into a package moved every consumer into a
module of its own. Sixty-four patch sites across four test files aimed at one
module object; after the split each has to name the submodule whose globals its
consumer reads at call time, and a patch that names the wrong one rebinds a name
nobody reads any more.

The invariant, the discovery scan and every check live in :mod:`tests._seam_guard`,
which this guard shares with ``tests/unit/video/test_mux_seams.py`` and
``tests/unit/recorder/test_compile_seams.py``. Read that module first; this file
only says what is specific to ``recorder.render``.

**What is specific to this package: it is the only one with both modes at once.**
Nine of the nineteen patched names are defined *inside* the package
(``_render_step``, ``_pace_narration``, ``_apply_timeline_edits``,
``_assemble_audio_tracks``, ``_publish_render_artifacts``,
``_prepare_main_after_popup_close``, ``_AUDIO_BED_CONCURRENCY``,
``_POPUP_CONTENT_BOX_TIMEOUT``, ``_POPUP_REQUEST_LOOKUP_TIMEOUT``) — those are
called through the module object, patched on the module that *defines* them, and
may not be name-imported anywhere. The other ten come from outside
(``Overlay``, ``Recorder``, ``SlideOverlay``, ``compose_popup_video``,
``detect_content_crop``, ``mux_audio_tracks``, ``build_audio_bed``,
``build_sfx_bed``, ``apply_time_edits``, ``probe_frame_count``) — those keep
``from X import name`` in the consuming submodule, which is where the patch has to
land, and there the name-import **is** the seam. ``video.mux`` has only the first
mode and ``recorder.compile`` only the second; mixing them up misses silently,
which is why the shared helper keys every decision on the definition site.

**Two names have two consumers, and one line no longer covers them.**
``Recorder`` is constructed in ``_run`` (the render loop) *and* in ``visuals``
(the post-popup-close funnel); ``probe_frame_count`` is read in ``timeline`` (the
model-versus-file check) *and* in ``_run`` (sizing the timeline). A test that
patches one of the two leaves the other path unintercepted, still green, and
checking nothing on that half —
:meth:`~tests._seam_guard.SeamGuard.assert_multi_consumer_coverage` is what turns
that from prose into a failure.

**One seam is a module deeper than the package.**
``_publish_render_artifacts`` commits the master MP4 with ``os.replace``, and the
rollback test replaces it through ``guidebot_recorder.recorder.render.audio.os.replace``
— ``audio``'s own ``os`` global, because ``audio`` is the module that performs the
rename. That is the same late-binding rule one level out, and the shared helper
accepts it only when every step of the chain really resolves to a module.

Why this matters more here than anywhere else: several of these seams sit on
paths whose assertions survive a dead patch. Seven tests shrink
``_POPUP_REQUEST_LOOKUP_TIMEOUT`` to fractions of a second and then assert an
*upper* bound of several seconds — a patch that reaches nobody makes them slow,
not red. Two more replace ``detect_content_crop`` with a function that fails the
test if called, which a dead patch turns into an assertion about nothing.

**The seam list is discovered, not written down here.** It is read out of
``tests/`` by finding what is actually patched on the render package, so a seam
added tomorrow is covered the day it appears — a hardcoded list would rot exactly
when it starts mattering. Needs no browser and no ffmpeg: this only parses source
and reads the package's runtime attributes.
"""

from __future__ import annotations

from tests._seam_guard import SeamGuard

PACKAGE_NAME = "guidebot_recorder.recorder.render"

GUARD = SeamGuard.build(PACKAGE_NAME)


def test_the_scan_is_complete() -> None:
    # Non-emptiness is not liveness. One recognised idiom in one file would make
    # `assert SEAMS` pass while the other sixty sites went unscanned, and every
    # assertion below would be vacuously true — the failure this guard exists to
    # catch. So an unclassifiable target that mentions the package is an error.
    GUARD.assert_scan_complete()


def test_every_patch_targets_a_submodule_not_the_facade() -> None:
    # Patching the facade cannot work: the consumer lives in a submodule and reads
    # that submodule's globals. Which submodule that is depends on where the name
    # is defined, and this package has both cases — see the module docstring.
    GUARD.assert_patch_targets()


def test_facade_withholds_every_patched_name() -> None:
    # Re-exporting a seam would let a patch on the facade succeed and reach
    # nobody. Withholding turns the same mistake into an AttributeError — or, for
    # the five names `test_render.py` used to import from the facade, into an
    # ImportError at collection time, which is louder still.
    GUARD.assert_facade_withholds()


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into the wrong module's globals, all
    # equally fatal and all invisible to the patch the tests actually write:
    # `from X import seam`, `alias = X.seam`, and `def f(..., _seam=X.seam)`.
    # This is also what keeps `Overlay` and `Recorder` out of the submodules that
    # only need them for a type annotation — they are annotated through the module
    # object precisely so no unreachable copy exists.
    GUARD.assert_no_early_binding()


def test_multi_consumer_seams_are_patched_on_every_consumer() -> None:
    # `Recorder` (`_run` + `visuals`) and `probe_frame_count` (`timeline` + `_run`)
    # each have two consumers after the split. One patch line covers one copy; the
    # other interception path would disappear with no test turning red.
    GUARD.assert_multi_consumer_coverage()
