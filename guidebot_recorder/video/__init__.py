"""Video montage: Playwright recording + one or more TTS audio beds via ffmpeg.

Fail-loud subprocess wrappers around ffmpeg/ffprobe. Explicit sample rate 48000.
"""

from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import MuxAudioTrack, mux, mux_audio_tracks

# `probe_duration` is a test seam and the mux facade withholds it on purpose, so
# it comes from its defining module. This one line is the single sanctioned
# name-import of a seam: it is a re-export of this package's public API, not a
# call site, and a re-export cannot be late-bound. Nothing patches it here. Any
# module that *calls* the seam must go through `probe.probe_duration(...)` —
# see tests/unit/video/test_mux_seams.py.
from guidebot_recorder.video.mux.probe import probe_duration

__all__ = [
    "MuxAudioTrack",
    "Placed",
    "build_audio_bed",
    "mux",
    "mux_audio_tracks",
    "probe_duration",
]
