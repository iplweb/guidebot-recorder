"""Video montage: Playwright recording + one or more TTS audio beds via ffmpeg.

Fail-loud subprocess wrappers around ffmpeg/ffprobe. Explicit sample rate 48000.
"""

from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import MuxAudioTrack, mux, mux_audio_tracks, probe_duration

__all__ = [
    "MuxAudioTrack",
    "Placed",
    "build_audio_bed",
    "mux",
    "mux_audio_tracks",
    "probe_duration",
]
