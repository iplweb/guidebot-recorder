"""ffprobe, FFmpeg video assembly, and audio muxing helpers.

All helpers are fail-loud: a missing binary or a non-zero exit raises immediately
(no silent fallbacks, per the design's fail-loud rule). The one deliberate
exception is :mod:`guidebot_recorder.video.mux.crop`, whose heuristics degrade to
"no crop" instead of aborting a render.

**This module is a facade and holds no logic.** It re-exports the names other
packages use so that ``from guidebot_recorder.video.mux import ...`` keeps working
after the split into submodules.

It also, on purpose, **withholds three names**: ``_run`` and ``_run_to_output``
(:mod:`~guidebot_recorder.video.mux.ffmpeg`) and ``probe_duration``
(:mod:`~guidebot_recorder.video.mux.probe`). Those three are the package's test
seams. Re-exporting them would let ``monkeypatch.setattr(mux, "_run", ...)``
succeed while reaching nobody — the patched consumer lives in a submodule and
resolves the name from *its own* globals — and a silently dead patch leaves tests
green while they check nothing. Withholding turns the same mistake into an
immediate ``AttributeError``/``ImportError``. Do **not** "fix" that by adding the
missing re-export; import the owning submodule instead::

    from guidebot_recorder.video.mux.probe import probe_duration     # consumers
    monkeypatch.setattr(mux_module.ffmpeg, "_run", fake)             # tests

``ffmpeg`` and ``probe`` are re-exported as *modules* for exactly that reason.

``_probe_all`` looks like it breaks that rule — :mod:`~guidebot_recorder.video.mux.crop`
and :mod:`~guidebot_recorder.video.mux.plan` import it by name, and this facade
re-exports it. It is not an oversight: ``_probe_all`` is not a seam, because
nothing patches it. Nor can it quietly become one, which is what makes leaving it
alone safe — the moment a test writes ``monkeypatch.setattr(mux_module, "_probe_all", ...)``
the seam scan picks the name up and ``test_facade_withholds_every_patched_name``
fails on this very re-export, before the dead patch can leave anything green.
"""

from __future__ import annotations

from . import ffmpeg, probe
from .compose import compose_popup_video
from .crop import (
    CROPDETECT_LIMIT,
    CROPDETECT_MIN_AGREEMENT,
    CROPDETECT_SAMPLES,
    CROPDETECT_TIMEOUT,
    TEARDOWN_TAIL_MAX_FRACTION,
    detect_content_crop,
    detect_teardown_tail,
)
from .ffmpeg import SAMPLE_RATE, ffmpeg_bin, ffprobe_bin
from .probe import _probe_all
from .tracks import FadeSpec, MuxAudioTrack, mux, mux_audio_tracks, mux_preencoded

__all__ = [
    "CROPDETECT_LIMIT",
    "CROPDETECT_MIN_AGREEMENT",
    "CROPDETECT_SAMPLES",
    "CROPDETECT_TIMEOUT",
    "SAMPLE_RATE",
    "TEARDOWN_TAIL_MAX_FRACTION",
    "FadeSpec",
    "MuxAudioTrack",
    "_probe_all",
    "compose_popup_video",
    "detect_content_crop",
    "detect_teardown_tail",
    "ffmpeg",
    "ffmpeg_bin",
    "ffprobe_bin",
    "mux",
    "mux_audio_tracks",
    "mux_preencoded",
    "probe",
]
