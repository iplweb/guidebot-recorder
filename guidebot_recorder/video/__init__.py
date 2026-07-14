"""Video montage: Playwright screen recording + TTS audio bed, muxed with ffmpeg.

Fail-loud subprocess wrappers around ffmpeg/ffprobe. Explicit sample rate 48000.
"""

from guidebot_recorder.video.audiobed import Placed, build_audio_bed
from guidebot_recorder.video.mux import mux, probe_duration

__all__ = ["Placed", "build_audio_bed", "mux", "probe_duration"]
