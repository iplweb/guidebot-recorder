"""ffmpeg-backed tests for probe_duration + mux (marked ``ffmpeg``).

Input material is generated with ffmpeg's ``testsrc``/``sine`` lavfi sources, so
the tests need no fixtures on disk. They are skipped when ffmpeg/ffprobe are not
installed (no shared conftest by design).
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import (
    MuxAudioTrack,
    compose_popup_video,
    mux,
    mux_audio_tracks,
    mux_preencoded,
    probe_duration,
)

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]


def _make_video(path: Path, seconds: float) -> None:
    """Write an H.264 mp4 (video only) of *seconds* duration."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_audio(path: Path, seconds: float) -> None:
    """Write a mono WAV tone of *seconds* duration."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate=48000",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_color_video(path: Path, color: str, seconds: float) -> None:
    """Write a solid-colour H.264 video."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:duration={seconds}:size=320x240:rate=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_main_color_timeline(path: Path) -> None:
    """Write red (0-1s), green (1-2s), then blue (2-3s)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:duration=1:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x00ff00:duration=1:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:duration=1:size=320x240:rate=25",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[outv]",
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_popup_with_bad_leading_frames(path: Path) -> None:
    """Write magenta pre-prime frames followed by a verified yellow interval."""

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=magenta:duration=0.2:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "color=c=yellow:duration=0.8:size=320x240:rate=25",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[outv]",
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_sparse_vfr_main(path: Path) -> None:
    """Write a 3s green VFR webm whose [1s, 2s) interval has *no* frames.

    Mimics a backgrounded main page: Playwright's VFR screencast can emit zero
    frames while the popup is on top, so a raw ``trim`` of the interval yields an
    empty backdrop. Only CFR normalisation (``fps``) fills it by cloning.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x00ff00:duration=3:size=320x240:rate=25",
            "-vf",
            "select='lt(t,1)+gte(t,2)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "1M",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _sample_rgb(path: Path, at: float) -> tuple[int, int, int]:
    """Decode one frame and return its average RGB colour."""
    return _sample_region_rgb(path, at, None)


def _sample_region_rgb(
    path: Path, at: float, crop: str | None
) -> tuple[int, int, int]:
    """Decode one frame (optionally cropped to *crop*) and average it to one RGB.

    *crop* is an ffmpeg ``crop`` spec ``w:h:x:y`` selecting a region before the
    1x1 area downscale, so callers can probe the composite's centre (popup) vs.
    its border (dimmed main) independently.
    """
    vf = "scale=1:1:flags=area" if crop is None else f"crop={crop},scale=1:1:flags=area"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            str(at),
            "-frames:v",
            "1",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    assert len(proc.stdout) == 3
    return tuple(proc.stdout)


# Regions of the 320x240 frame: the popup centre and a left border strip that
# floating mode leaves as (dimmed) backdrop at scale=0.72.
_CENTER = "40:40:140:100"
_BORDER = "10:240:0:0"


def _assert_rgb(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
    assert actual == pytest.approx(expected, abs=20)


def _stream_types(path: Path) -> list[str]:
    """Return the codec_type of each stream in *path* (via ffprobe)."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()


def _video_codec(path: Path) -> str:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _audio_streams(path: Path) -> list[dict]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index,codec_name,sample_rate,channels:"
            "stream_tags=language,title,handler_name:stream_disposition=default",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)["streams"]


def test_probe_duration_matches(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    _make_video(video, 2.0)
    assert probe_duration(video) == pytest.approx(2.0, abs=0.3)


def test_probe_duration_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_duration(tmp_path / "nope.mp4")


def test_atomic_output_preserves_previous_artifact_after_ffmpeg_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.mp4"
    out.write_bytes(b"previous-good-artifact")

    def fail_after_partial_write(cmd: list[str]):
        Path(cmd[-1]).write_bytes(b"partial")
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(mux_module, "_run", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        mux_module._run_to_output(["ffmpeg", "-y"], out)

    assert out.read_bytes() == b"previous-good-artifact"
    assert list(tmp_path.glob(".out.*.mp4")) == []


def test_mux_produces_one_video_one_audio(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 2.0)
    _make_audio(audio, 2.0)

    mux(video, audio, out)

    assert out.exists()
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1
    assert len(types) == 2
    # -shortest keeps the muxed file close to the (equal) input lengths.
    assert probe_duration(out) == pytest.approx(2.0, abs=0.4)


def test_mux_shortest_clips_to_shorter_stream(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 3.0)
    _make_audio(audio, 1.0)

    mux(video, audio, out)

    # -shortest → output no longer than the 1s audio.
    assert probe_duration(out) == pytest.approx(1.0, abs=0.5)


def test_mux_audio_tracks_embeds_languages_titles_and_default_disposition(
    tmp_path: Path,
) -> None:
    video = tmp_path / "v.mp4"
    polish = tmp_path / "pl.wav"
    english = tmp_path / "en.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 2.0)
    _make_audio(polish, 2.0)
    _make_audio(english, 2.0)

    mux_audio_tracks(
        video,
        [
            MuxAudioTrack(polish, language="pol", title="Polski", default=True),
            MuxAudioTrack(english, language="eng", title="English"),
        ],
        out,
    )

    assert _stream_types(out) == ["video", "audio", "audio"]
    streams = _audio_streams(out)
    assert [stream["codec_name"] for stream in streams] == ["aac", "aac"]
    assert [stream["sample_rate"] for stream in streams] == ["48000", "48000"]
    assert [stream["channels"] for stream in streams] == [2, 2]
    assert [stream["tags"]["language"] for stream in streams] == ["pol", "eng"]
    assert [stream["tags"]["handler_name"] for stream in streams] == ["Polski", "English"]
    assert [stream["disposition"]["default"] for stream in streams] == [1, 0]
    payload = out.read_bytes()
    assert payload.find(b"moov") < payload.find(b"mdat")


def test_mux_audio_tracks_requires_exactly_one_default(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    _make_video(video, 1.0)
    _make_audio(audio, 1.0)

    with pytest.raises(ValueError, match="exactly one default"):
        mux_audio_tracks(
            video,
            [MuxAudioTrack(audio, language="pol")],
            tmp_path / "out.mp4",
        )


def test_mux_audio_tracks_rejects_unregistered_language_code(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    _make_video(video, 1.0)
    _make_audio(audio, 1.0)

    with pytest.raises(ValueError, match="registered ISO 639-2"):
        mux_audio_tracks(
            video,
            [MuxAudioTrack(audio, language="xyz", default=True)],
            tmp_path / "out.mp4",
        )


def test_mux_audio_tracks_rejects_audio_shorter_than_video(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    _make_video(video, 2.0)
    _make_audio(audio, 1.0)

    with pytest.raises(ValueError, match="duration.*does not match"):
        mux_audio_tracks(
            video,
            [MuxAudioTrack(audio, language="pol", default=True)],
            tmp_path / "out.mp4",
        )


def test_mux_audio_tracks_preencoded_copies_video_with_multiple_audio_streams(
    tmp_path: Path,
) -> None:
    video = tmp_path / "v.mp4"
    polish = tmp_path / "pl.wav"
    english = tmp_path / "en.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 2.0)
    _make_audio(polish, 2.0)
    _make_audio(english, 2.0)

    mux_audio_tracks(
        video,
        [
            MuxAudioTrack(polish, language="pol", title="Polski", default=True),
            MuxAudioTrack(english, language="eng", title="English"),
        ],
        out,
        preencoded=True,
    )

    assert _video_codec(out) == "h264"
    assert _stream_types(out) == ["video", "audio", "audio"]
    assert [stream["tags"]["language"] for stream in _audio_streams(out)] == [
        "pol",
        "eng",
    ]


def test_compose_popup_video_switches_main_popup_main(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_omits_tail_when_popup_stays_open(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=3.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    # The last second must still be the popup, not main's blue tail.
    _assert_rgb(_sample_rgb(out, 2.5), (255, 255, 0))


def test_compose_popup_video_pads_bounded_encoder_startup_gap(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.92)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 1.1), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 1.8), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_discards_frames_before_visual_prime(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_bad_leading_frames(popup)

    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.2,
        visual_ready_delay=0.4,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 1.3), (0, 255, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_rejects_large_encoder_gap(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_video(main, 4.0)
    _make_color_video(popup, "yellow", 0.5)

    with pytest.raises(ValueError, match="startup gap"):
        compose_popup_video(main, popup, out, opened_at=0.5, closed_at=3.5)


def _assert_dimmed_green(rgb: tuple[int, int, int]) -> None:
    """Assert *rgb* reads as backdrop green darkened by the dim ramp."""
    red, green, blue = rgb
    assert green > 50, f"backdrop should still be visibly green: {rgb}"
    assert green < 210, f"backdrop should be dimmed, not full green: {rgb}"
    assert red < 70 and blue < 70, f"backdrop should be green-dominant: {rgb}"


def _assert_yellow(rgb: tuple[int, int, int]) -> None:
    red, green, blue = rgb
    assert red > 170 and green > 170 and blue < 90, f"expected popup yellow: {rgb}"


def test_compose_popup_video_floating_composites_popup_over_dimmed_main(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    # Full-length film, one H.264 encode.
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    # Pre/tail are the verbatim main page.
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    # The interval is a composite: scaled popup inset, dimmed main at the border.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_floating_cfr_fills_empty_backdrop(tmp_path: Path) -> None:
    main = tmp_path / "main.webm"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_sparse_vfr_main(main)  # no frames in [1s, 2s)
    _make_color_video(popup, "yellow", 1.0)

    # A raw trim of the interval would be empty; CFR normalisation must fill it,
    # so this renders without the empty-backdrop guard firing.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # The backdrop is present for the full interval (cloned last real frame).
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))


def test_compose_popup_video_floating_no_pre(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Popup opens at t~0: no pre segment, mid + tail only.
    compose_popup_video(main, popup, out, opened_at=0.0, closed_at=1.0, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 0.5, _CENTER))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_floating_no_tail_holds_open(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    # Popup open to end-of-main: no tail, hold the framed popup (no phantom close).
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=2.0,
        closed_at=3.0,
        floating=True,
        hold_open_at_end=True,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    # Held open at the last frame: still the framed popup, still dimmed backdrop.
    # The mid interval [2s, 3s) is main's blue segment, dimmed by the ramp.
    _assert_yellow(_sample_region_rgb(out, 2.9, _CENTER))
    red, green, blue = _sample_region_rgb(out, 2.9, _BORDER)
    assert blue > 50 and blue < 245, f"backdrop blue should be dimmed: {(red, green, blue)}"
    assert red < 70 and green < 70, f"backdrop should stay blue-dominant: {(red, green, blue)}"


def test_compose_popup_video_floating_clamps_short_transition(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.5)

    # span (0.3s) < open_ms + close_ms (0.56s): the fades must clamp, not overrun.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=1.3, floating=True)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_floating_false_is_a_hard_cut(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Default floating=False: the interval is a full-frame popup, no backdrop.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=False)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_mux_preencoded_adds_audio_without_changing_video_codec(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 2.0)
    _make_audio(audio, 2.0)

    mux_preencoded(video, audio, out)

    assert _video_codec(out) == "h264"
    assert _stream_types(out) == ["video", "audio"]
    assert probe_duration(out) == pytest.approx(2.0, abs=0.4)
