"""ffprobe, FFmpeg video assembly, and audio muxing helpers.

All helpers are fail-loud: a missing binary or a non-zero exit raises immediately
(no silent fallbacks, per the design's fail-loud rule).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from guidebot_recorder.languages import is_iso_639_2

#: Audio sample rate used everywhere in the montage pipeline (design §8).
SAMPLE_RATE = 48000


@dataclass(frozen=True, slots=True)
class MuxAudioTrack:
    """One audio input and the metadata of its MP4 stream."""

    path: Path
    language: str
    title: str | None = None
    default: bool = False


def _resolve(binary: str) -> str:
    """Return the absolute path to *binary* or raise a loud error."""
    found = shutil.which(binary)
    if found is None:
        raise RuntimeError(
            f"'{binary}' not found on PATH. Install ffmpeg "
            "(e.g. `brew install ffmpeg`) to render/mux video."
        )
    return found


def ffmpeg_bin() -> str:
    """Path to the ffmpeg executable (fail-loud if absent)."""
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    """Path to the ffprobe executable (fail-loud if absent)."""
    return _resolve("ffprobe")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output; raise ``RuntimeError`` on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _run_to_output(cmd: list[str], out: Path) -> None:
    """Run an ffmpeg command atomically, appending a temporary output path.

    The temporary file lives beside the final artifact so ``os.replace`` is atomic.
    A failed command never truncates a previously successful MP4/WAV.
    """

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{out.stem}.",
        suffix=out.suffix,
        dir=out.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _run([*cmd, str(temporary)])
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)


def probe_duration(path: Path) -> float:
    """Return the container duration of *path* in seconds via ffprobe.

    Raises ``FileNotFoundError`` if the file is missing and ``RuntimeError`` if
    ffprobe cannot report a numeric duration.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    raw = proc.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"ffprobe returned non-numeric duration: {raw!r}") from exc


def _check_sources(*paths: Path) -> None:
    """Raise before invoking ffmpeg when any input is missing."""
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)


def compose_popup_video(
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    *,
    visual_ready_delay: float = 0.0,
) -> None:
    """Cut between the main-page and popup recordings on one timeline.

    ``opened_at`` and ``closed_at`` are offsets on the main recording's clock.
    ``visual_ready_delay`` is the bounded time from the page event until a frame
    with both visual layers was verified. The resulting picture keeps main on
    screen through that delay, trims any earlier popup frames, then follows the
    popup until ``closed_at`` and returns to main. When ``closed_at`` is the end
    of ``main`` the final segment is omitted.

    Playwright gives every page in the context the same configured frame size,
    so no scaling is applied.  Each segment has its timestamps reset before the
    concat filter and the final stream is encoded once as H.264 for MP4.
    """
    main, popup, out = Path(main), Path(popup), Path(out)
    _check_sources(main, popup)

    opened_at = float(opened_at)
    closed_at = float(closed_at)
    visual_ready_delay = float(visual_ready_delay)
    if not all(math.isfinite(value) for value in (opened_at, closed_at, visual_ready_delay)):
        raise ValueError("popup timestamps must be finite")
    if opened_at < 0:
        raise ValueError(f"opened_at must be >= 0, got {opened_at}")
    if closed_at <= opened_at:
        raise ValueError(f"closed_at must be greater than opened_at, got {opened_at}..{closed_at}")
    if visual_ready_delay < 0:
        raise ValueError(f"visual_ready_delay must be >= 0, got {visual_ready_delay}")

    main_duration = probe_duration(main)
    # Container durations are frame-rounded.  Accept a sub-frame overshoot from
    # the monotonic browser clock, but fail loudly on a genuinely invalid range.
    tolerance = 0.05
    if opened_at > main_duration + tolerance:
        raise ValueError(f"opened_at ({opened_at}) is past main video duration ({main_duration})")
    if closed_at > main_duration + tolerance:
        raise ValueError(f"closed_at ({closed_at}) is past main video duration ({main_duration})")
    opened_at = min(opened_at, main_duration)
    closed_at = min(closed_at, main_duration)
    raw_popup_span = closed_at - opened_at
    if raw_popup_span <= 0:
        raise ValueError("popup interval has no encoded video frames")
    if visual_ready_delay >= raw_popup_span:
        raise ValueError("visual-ready delay consumes the whole popup interval")
    popup_duration = probe_duration(popup)
    encoder_startup_gap = max(0.0, raw_popup_span - popup_duration)
    # Page events precede the popup encoder's first frame. Real Chromium startup
    # can take a couple of seconds; permit that floor or 15% on longer intervals,
    # while rejecting a mismatch large enough to describe a different timeline.
    max_startup_gap = max(2.0, raw_popup_span * 0.15)
    if encoder_startup_gap > max_startup_gap:
        raise ValueError(
            f"popup encoder startup gap ({encoder_startup_gap}) exceeds limit ({max_startup_gap})"
        )

    # Playwright resets each WebM's PTS to zero, so container duration cannot
    # reveal which early raw frames preceded the verified visual-ready point.
    # Conservatively trim the full wall-clock prime delay from the source. This
    # may discard a few already-good frames, but guarantees that tpad can only
    # clone a post-prime frame.
    popup_source_start = visual_ready_delay
    opened_at += visual_ready_delay
    popup_span = closed_at - opened_at
    popup_available = popup_duration - popup_source_start
    if popup_span <= 0 or popup_available <= 0:
        raise ValueError("popup has no verified encoded video frames")
    startup_gap = max(0.0, popup_span - popup_available)
    popup_cut_duration = min(popup_span, popup_available)

    has_pre = opened_at > tolerance
    has_tail = main_duration - closed_at > tolerance

    filters: list[str] = []
    main_sources: dict[str, str] = {}
    if has_pre and has_tail:
        filters.append("[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]")
        main_sources = {"pre": "[main_pre_src]", "tail": "[main_tail_src]"}
    elif has_pre:
        main_sources = {"pre": "[0:v]"}
    elif has_tail:
        main_sources = {"tail": "[0:v]"}

    labels: list[str] = []
    if has_pre:
        source = main_sources["pre"]
        normalize = "" if has_tail else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start=0:end={opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )
        labels.append("[main_pre]")

    popup_filter = (
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,"
        f"trim=start={popup_source_start:.6f}:"
        f"end={popup_source_start + popup_cut_duration:.6f},"
        "setpts=PTS-STARTPTS"
    )
    if startup_gap > tolerance:
        popup_filter += f",tpad=start_mode=clone:start_duration={startup_gap:.6f}"
    popup_filter += (
        f",trim=duration={popup_span:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]"
    )
    filters.append(popup_filter)
    labels.append("[popup_cut]")

    if has_tail:
        source = main_sources["tail"]
        normalize = "" if has_pre else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start={closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )
        labels.append("[main_tail]")

    if len(labels) == 1:
        filters.append(f"{labels[0]}null[outv]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

    _run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(main),
            "-i",
            str(popup),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ],
        out,
    )


def mux(video: Path, audio: Path, out: Path) -> None:
    """Combine *video* and *audio* into *out*.

    Video is transcoded to H.264 (Playwright records VP8/WebM, which the MP4
    container does not accept — a stream copy would fail); audio is encoded to
    AAC at the canonical 48000 Hz sample rate. ``-shortest`` clips output to the
    shorter of the two streams so the audio bed never runs past the recording.
    """
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    _run_to_output(
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


def mux_audio_tracks(
    video: Path,
    tracks: list[MuxAudioTrack],
    out: Path,
    *,
    preencoded: bool = False,
) -> None:
    """Attach one or more language-tagged audio tracks to a single MP4 video.

    The first track must be the sole default stream. Every audio bed must already
    match the video duration; the video clock is authoritative and ``-shortest``
    is deliberately avoided so a malformed short track cannot truncate the film.
    ``preencoded`` copies an already H.264-compatible picture (the popup
    compositor path); otherwise Playwright's WebM picture is encoded to H.264.
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
    video_duration = probe_duration(video)
    duration_tolerance = 0.05
    for track in tracks:
        audio_duration = probe_duration(track.path)
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
    if preencoded:
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
    _run_to_output(cmd, out)


def mux_preencoded(video: Path, audio: Path, out: Path) -> None:
    """Attach audio to an MP4-compatible video without re-encoding its picture."""
    video, audio, out = Path(video), Path(audio), Path(out)
    _check_sources(video, audio)
    _run_to_output(
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
