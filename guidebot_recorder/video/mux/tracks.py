"""Muxing finished picture with one or more audio beds, and the fade at both ends.

The end of the pipeline: everything here takes an already-composed picture and
attaches sound. :func:`mux` and :func:`mux_preencoded` are the single-track forms
(encode vs. stream-copy the picture); :func:`mux_audio_tracks` is the multi-language
form that also enforces the track contract (exactly one default, first; registered
ISO 639-2 codes; every bed matching the video duration). :class:`FadeSpec` and
:func:`_fade_filters` live here rather than in a module of their own because the
fade is only ever applied on this path — and applying one forces the encode, which
is why it is opt-in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from guidebot_recorder.languages import is_iso_639_2

from . import ffmpeg, probe
from .ffmpeg import SAMPLE_RATE, ffmpeg_bin
from .probe import _check_sources


@dataclass(frozen=True, slots=True)
class MuxAudioTrack:
    """One audio input and the metadata of its MP4 stream."""

    path: Path
    language: str
    title: str | None = None
    default: bool = False


def mux(video: Path, audio: Path, out: Path) -> None:
    """Combine *video* and *audio* into *out*.

    Video is transcoded to H.264 (Playwright records VP8/WebM, which the MP4
    container does not accept — a stream copy would fail); audio is encoded to
    AAC at the canonical 48000 Hz sample rate. ``-shortest`` clips output to the
    shorter of the two streams so the audio bed never runs past the recording.
    """
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    ffmpeg._run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            str(SAMPLE_RATE),
            "-shortest",
        ],
        out,
    )


@dataclass(frozen=True)
class FadeSpec:
    """A fade from/to a flat colour at the two ends of the finished film.

    Durations are seconds and either may be zero. ``audio`` fades every narration
    bed in step with the picture, which is almost always what a fade to black
    wants — see :class:`~guidebot_recorder.models.config.FadeConfig`.
    """

    fade_in: float = 0.0
    fade_out: float = 0.0
    color: str = "black"
    audio: bool = True

    def is_noop(self) -> bool:
        return self.fade_in <= 0 and self.fade_out <= 0


def _fade_filters(fade: FadeSpec, duration: float) -> tuple[str, str | None]:
    """Build the ``(video, audio)`` filter chains for *fade* over *duration*.

    The out-fade is anchored to the film's end here rather than by the caller, so
    it lands correctly whatever the composite turned out to be. The audio chain
    is ``None`` when the fade is picture-only.
    """
    video_parts: list[str] = []
    audio_parts: list[str] = []
    if fade.fade_in > 0:
        video_parts.append(f"fade=t=in:st=0:d={fade.fade_in:.6f}:c={fade.color}")
        audio_parts.append(f"afade=t=in:st=0:d={fade.fade_in:.6f}")
    if fade.fade_out > 0:
        start = max(0.0, duration - fade.fade_out)
        video_parts.append(f"fade=t=out:st={start:.6f}:d={fade.fade_out:.6f}:c={fade.color}")
        audio_parts.append(f"afade=t=out:st={start:.6f}:d={fade.fade_out:.6f}")
    return ",".join(video_parts), (",".join(audio_parts) if fade.audio and audio_parts else None)


def mux_audio_tracks(
    video: Path,
    tracks: list[MuxAudioTrack],
    out: Path,
    *,
    preencoded: bool = False,
    video_duration: float | None = None,
    fade: FadeSpec | None = None,
) -> None:
    """Attach one or more language-tagged audio tracks to a single MP4 video.

    The first track must be the sole default stream. Every audio bed must already
    match the video duration; the video clock is authoritative and ``-shortest``
    is deliberately avoided so a malformed short track cannot truncate the film.
    ``preencoded`` copies an already H.264-compatible picture (the popup
    compositor path); otherwise Playwright's WebM picture is encoded to H.264.
    Callers that just probed an immutable staged video may pass ``video_duration``
    to avoid launching ffprobe for the same artifact again.

    ``fade`` ramps the picture (and, unless opted out, every audio bed) at both
    ends. A filter cannot be applied to a copied stream, so requesting one forces
    the encode even on the ``preencoded`` path — the reason fades are opt-in.
    """

    video, out = Path(video), Path(out)
    tracks = [
        MuxAudioTrack(
            path=Path(track.path),
            language=track.language,
            title=track.title,
            default=track.default,
        )
        for track in tracks
    ]
    if not tracks:
        raise ValueError("at least one audio track is required")
    default_indices = [index for index, track in enumerate(tracks) if track.default]
    if default_indices != [0]:
        raise ValueError("exactly one default audio track is required and it must be first")
    languages = [track.language for track in tracks]
    if any(not is_iso_639_2(language) for language in languages):
        raise ValueError("audio track language must be a registered ISO 639-2 code")
    if len(languages) != len(set(languages)):
        raise ValueError("audio track languages must be unique")

    _check_sources(video, *(track.path for track in tracks))
    if video_duration is None:
        video_duration = probe.probe_duration(video)
    elif not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError("video_duration must be finite and positive")
    duration_tolerance = 0.05
    for track in tracks:
        audio_duration = probe.probe_duration(track.path)
        if abs(audio_duration - video_duration) > duration_tolerance:
            raise ValueError(
                f"audio track {track.language} duration ({audio_duration}) does not match "
                f"video duration ({video_duration})"
            )

    cmd = [ffmpeg_bin(), "-y", "-i", str(video)]
    for track in tracks:
        cmd += ["-i", str(track.path)]
    cmd += ["-map", "0:v:0"]
    for input_index in range(1, len(tracks) + 1):
        cmd += ["-map", f"{input_index}:a:0"]
    fade_active = fade is not None and not fade.is_noop()
    if fade_active:
        assert fade is not None
        if fade.fade_in + fade.fade_out > video_duration:
            raise ValueError(
                f"fade ({fade.fade_in} + {fade.fade_out}) is longer than the film "
                f"({video_duration})"
            )
        video_chain, audio_chain = _fade_filters(fade, video_duration)
        # A filtered stream cannot also be copied, so `preencoded` is overridden
        # rather than silently dropping the fade the scenario asked for.
        cmd += ["-vf", video_chain, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio_chain is not None:
            for stream_index in range(len(tracks)):
                cmd += [f"-filter:a:{stream_index}", audio_chain]
    elif preencoded:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += [
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-b:a",
        "192k",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    for stream_index, track in enumerate(tracks):
        title = track.title or track.language
        cmd += [
            f"-metadata:s:a:{stream_index}",
            f"language={track.language}",
            f"-metadata:s:a:{stream_index}",
            f"title={title}",
            f"-metadata:s:a:{stream_index}",
            f"handler_name={title}",
            f"-disposition:a:{stream_index}",
            "default" if track.default else "0",
        ]
    cmd += [
        "-movflags",
        "+faststart",
        "-t",
        f"{video_duration:.6f}",
    ]
    ffmpeg._run_to_output(cmd, out)


def mux_preencoded(video: Path, audio: Path, out: Path) -> None:
    """Attach audio to an MP4-compatible video without re-encoding its picture."""
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    ffmpeg._run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            str(SAMPLE_RATE),
            "-shortest",
        ],
        out,
    )
