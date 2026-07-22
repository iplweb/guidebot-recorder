"""Video montage: Playwright recording + one or more TTS audio beds via ffmpeg.

Fail-loud subprocess wrappers around ffmpeg/ffprobe. Explicit sample rate 48000.
"""

from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import MuxAudioTrack, mux, mux_audio_tracks

# `probe_duration` is deliberately NOT re-exported here. It is a test seam, and a
# re-export would launder it: any module could write
# `from guidebot_recorder.video import probe_duration`, call it bare, and the
# `mux_module.probe` patch would reach nobody — while the seam guard stayed green,
# because `guidebot_recorder.video` contains no "mux" for its scan to notice.
# Callers import the defining module and call `probe.probe_duration(...)`.
# See tests/unit/video/test_mux_seams.py.

__all__ = [
    "MuxAudioTrack",
    "Placed",
    "build_audio_bed",
    "mux",
    "mux_audio_tracks",
]
