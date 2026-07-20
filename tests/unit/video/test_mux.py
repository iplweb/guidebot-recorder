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
    detect_content_crop,
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


def _sample_region_rgb(path: Path, at: float, crop: str | None) -> tuple[int, int, int]:
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


def _frame_count(path: Path) -> int:
    """Return the decoded video frame count of *path* (via ffprobe)."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip())


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


def test_probe_all_is_fresh_after_input_is_rewritten(tmp_path: Path) -> None:
    video = tmp_path / "rewritten.mp4"
    _make_video(video, 1.0)

    first = mux_module._probe_all(video)

    _make_video(video, 2.0)
    second = mux_module._probe_all(video)

    assert first.duration == pytest.approx(1.0, abs=0.3)
    assert second.duration == pytest.approx(2.0, abs=0.3)


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


def test_mux_audio_tracks_reuses_known_video_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    out = tmp_path / "out.mp4"
    _make_video(video, 1.0)
    _make_audio(audio, 1.0)
    probed: list[Path] = []
    original_probe = mux_module.probe_duration

    def recording_probe(path: Path) -> float:
        probed.append(Path(path))
        return original_probe(path)

    monkeypatch.setattr(mux_module, "probe_duration", recording_probe)

    mux_audio_tracks(
        video,
        [MuxAudioTrack(audio, language="pol", default=True)],
        out,
        video_duration=1.0,
    )

    assert probed == [audio]
    assert out.exists()


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


def test_compose_popup_video_floating_zero_transition_ms_renders(tmp_path: Path) -> None:
    # open_ms=0 (a valid "no open animation" config) must not make the dim ramp
    # divide by zero (t/0 -> inf/NaN brightness).
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, open_ms=0, close_ms=0
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))


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


# Two strips either side of the sliding boundary during a push (320px wide frame):
# a left strip (main, still on screen) and a right strip (popup, entering).
_LEFT_STRIP = "40:40:40:100"
_RIGHT_STRIP = "40:40:240:100"


def test_compose_popup_video_slide_pushes_in_holds_and_out(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)  # red 0-1s, green 1-2s, blue 2-3s
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=200
    )

    # Full-length film, one H.264 encode, CFR frame count.
    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    assert _video_codec(out) == "h264"
    assert _frame_count(out) == pytest.approx(round(25 * 3.0), abs=3)
    # Pre/tail are the verbatim main page.
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    # During the push-in a single frame shows BOTH layers: green main still on the
    # left, yellow popup entering on the right (a moving boundary, not a cut).
    _assert_rgb(_sample_region_rgb(out, 1.1, _LEFT_STRIP), (0, 255, 0))
    _assert_yellow(_sample_region_rgb(out, 1.1, _RIGHT_STRIP))
    # Mid hold is FULL-FRAME popup: centre AND border are both popup yellow
    # (unlike float, where the border stays dimmed main).
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_slide_composition_probes_each_artifact_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "out.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)
    probed_paths: list[Path] = []
    original_run = mux_module._run

    def recording_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if Path(cmd[0]).name == "ffprobe":
            probed_paths.append(Path(cmd[-1]))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(mux_module, "_run", recording_run)

    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.0,
        transition="slide",
    )

    assert probed_paths.count(main) == 1
    assert probed_paths.count(popup) == 1
    assert probed_paths.count(out) == 1


def test_compose_popup_video_slide_no_black_flash_at_push_out_end(tmp_path: Path) -> None:
    # A non-frame-aligned opened/closed leaves the CFR mid one frame short of the
    # colour base; with eof_action=pass the final mid frame flashed BLACK (the base
    # showing through) right before the tail. eof_action=repeat holds the last main
    # frame instead. Interval sits inside the green segment so the push-out returns
    # to green; assert no near-black frame across the tail of the push-out.
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)  # red 0-1, green 1-2, blue 2-3
    _make_color_video(popup, "yellow", 1.0)

    # These bounds are deliberately NOT frame-aligned (25fps): they leave the CFR
    # mid one frame short of the base, which is what triggered the flash. (Verified:
    # with eof_action=pass the frames at t≈1.97..1.988 render black.)
    compose_popup_video(
        main, popup, out, opened_at=1.01, closed_at=1.99, transition="slide", slide_ms=200
    )

    for offset in (0.02, 0.01, 0.005, 0.002):
        red, green, blue = _sample_region_rgb(out, 1.99 - offset, _CENTER)
        assert not (red < 40 and green < 40 and blue < 40), (
            f"black flash at t={1.99 - offset:.3f}: {(red, green, blue)}"
        )


def test_compose_popup_video_slide_tail_clock_alignment(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=200
    )

    # A frame sampled just after closed_at must equal main's colour at that time:
    # main is blue from 2s, so an offset/time-warp in the tail would show green.
    _assert_rgb(_sample_region_rgb(out, 2.05, None), (0, 0, 255))


def test_compose_popup_video_slide_hold_open_at_end(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 2.0)

    # Popup open to end-of-main: no tail, no push-out; hold full-frame to the end.
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=2.0,
        closed_at=3.0,
        transition="slide",
        slide_ms=200,
        hold_open_at_end=True,
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # Pre is verbatim main (red then green).
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 1.5, None), (0, 255, 0))
    # Last frame is full-frame popup (no push-out revealing main): centre + border.
    _assert_yellow(_sample_region_rgb(out, 2.9, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 2.9, _BORDER))


def test_compose_popup_video_slide_no_pre_renders(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # Popup opens at t~0: no pre segment, mid + tail only. Still pushes in.
    compose_popup_video(
        main, popup, out, opened_at=0.0, closed_at=1.0, transition="slide", slide_ms=200
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 0.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 0.5, _BORDER))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_slide_zero_ms_renders(tmp_path: Path) -> None:
    # slide_ms=0 (a valid "no slide" config) must not divide by zero (t/0): both
    # D_in and D_out collapse to 0, so prog is constant 1 (full-frame the whole mid).
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, transition="slide", slide_ms=0
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_slide_clamps_short_interval(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 0.5)

    # span (0.3s) < 2 x slide_ms (0.4s): D_in/D_out must clamp, not overrun.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=1.3, transition="slide", slide_ms=200
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))


def test_compose_popup_video_transition_cut_matches_default(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # transition="cut" reproduces today's hard cut (main -> full-frame popup -> main).
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, transition="cut")

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_rgb(out, 0.5), (255, 0, 0))
    _assert_rgb(_sample_rgb(out, 1.5), (255, 255, 0))
    _assert_rgb(_sample_rgb(out, 2.5), (0, 0, 255))


def test_compose_popup_video_transition_float_matches_floating(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # transition="float" reproduces Spec B: scaled popup inset over dimmed main.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, transition="float")

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    _assert_rgb(_sample_region_rgb(out, 0.5, None), (255, 0, 0))
    _assert_rgb(_sample_region_rgb(out, 2.5, None), (0, 0, 255))
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


# --- popup crop (the recorded popup canvas is the *main* viewport) -----------
# Playwright's record_video_size is context-level, so the popup records onto a
# full-viewport canvas: its real window sits top-left and the rest is filler.
# ``popup_crop`` trims that filler *before* the scale, so the rounded corners,
# fade and shadow are all computed on the real window.

# A region that lands inside the framed popup only once the filler is cropped
# away. Uncropped, the 320x240 popup scales to 230x172 centred at x=45..275 and
# the filler (source x>160 -> screen x>=160) covers this strip.
_CROPPED_RIGHT = "16:16:186:112"


def _make_popup_with_filler(path: Path, seconds: float) -> None:
    """Write a 320x240 popup whose real window is only the top-left 160x120.

    Mimics a popup recorded onto the main window's canvas: yellow content in the
    top-left corner, grey filler everywhere else.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x808080:duration={seconds}:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=160x120:rate=25",
            "-filter_complex",
            "[0:v][1:v]overlay=x=0:y=0,format=yuv420p[outv]",
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


def _popup_chain(filters: str) -> str:
    """Return the ``[popup_cut]`` consumer link of a filtergraph."""
    (chain,) = [part for part in filters.split(";") if part.startswith("[popup_cut]")]
    return chain


def _capture_filtergraph(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the ``-filter_complex`` of every ffmpeg run, then run it for real.

    Running it too keeps the assertions honest: a filtergraph that matches the
    expected string but that ffmpeg rejects still fails the test.
    """
    seen: list[str] = []
    real_run = mux_module._run_to_output

    def spy_run(cmd: list[str], out: Path) -> None:
        seen.append(cmd[cmd.index("-filter_complex") + 1])
        real_run(cmd, out)

    monkeypatch.setattr(mux_module, "_run_to_output", spy_run)
    return seen


def test_compose_popup_video_float_crops_popup_to_its_content(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(160, 120, 0, 0)
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.2)
    # The framed window is now wall-to-wall content: no grey filler inside it.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CENTER))
    _assert_yellow(_sample_region_rgb(out, 1.5, _CROPPED_RIGHT))
    # Outside the (now smaller) window the dimmed main page still shows.
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_float_without_crop_keeps_the_filler(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    # Back-compat: no geometry supplied -> today's full-canvas scaling, filler and all.
    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    rgb = _sample_region_rgb(out, 1.5, _CROPPED_RIGHT)
    red, green, blue = rgb
    assert not (red > 170 and green > 170 and blue < 90), f"expected grey filler: {rgb}"


def test_compose_popup_video_float_crop_precedes_scale_and_is_even(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = _capture_filtergraph(monkeypatch)

    # Odd numbers everywhere: yuv420p needs even dimensions, so they must snap down.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(161, 121, 3, 5)
    )

    chain = _popup_chain(seen[0])
    assert "crop=160:120:2:4," in chain
    assert chain.index("crop=") < chain.index("scale=")


def test_compose_popup_video_float_without_crop_emits_no_crop_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = _capture_filtergraph(monkeypatch)

    compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, floating=True)

    assert "crop=" not in _popup_chain(seen[0])


def test_compose_popup_video_float_full_frame_crop_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = _capture_filtergraph(monkeypatch)

    # A popup whose requested window is at least the whole canvas must not gain a
    # redundant crop filter.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(400, 300, 0, 0)
    )

    assert "crop=" not in _popup_chain(seen[0])


def test_compose_popup_video_cut_ignores_popup_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)
    seen = _capture_filtergraph(monkeypatch)

    # cut/slide show the popup full-frame; cropping is a float-only cosmetic.
    compose_popup_video(
        main,
        popup,
        out,
        opened_at=1.0,
        closed_at=2.0,
        transition="cut",
        popup_crop=(160, 120, 0, 0),
    )

    assert "crop=" not in seen[0]


def test_compose_popup_video_rejects_out_of_frame_crop(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    with pytest.raises(ValueError, match="popup_crop"):
        compose_popup_video(
            main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=(0, 120, 0, 0)
        )


# --- popup crop, level 3: the pixel heuristic --------------------------------
# When neither the window.open features nor the popup's own content bounding box
# state a geometry, the recording itself is the last witness: the real window is
# the region that is *not* the flat filler Playwright pads the canvas with.


def _make_popup_with_shifting_filler(path: Path, seconds: float) -> None:
    """Write a 320x240 popup whose content region changes size over time.

    No rect is stable across frames, so the consensus must be refused rather than
    letting one frame's answer decide the whole composite.
    """
    third = seconds / 3
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x808080:duration={seconds}:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=300x220:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=200x150:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=100x70:rate=25",
            "-filter_complex",
            (
                f"[0:v][1:v]overlay=x=0:y=0:enable='lt(t,{third:.3f})'[a];"
                f"[a][2:v]overlay=x=0:y=0:"
                f"enable='between(t,{third:.3f},{2 * third:.3f})'[b];"
                f"[b][3:v]overlay=x=0:y=0:enable='gt(t,{2 * third:.3f})',"
                "format=yuv420p[outv]"
            ),
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


def test_detect_content_crop_finds_the_window_inside_flat_filler(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.2)

    # The filler is mid-grey, not black, so a plain cropdetect would see nothing:
    # the detection keys on "differs from the padding colour", not on darkness.
    assert detect_content_crop(popup) == (160, 120, 0, 0)


def test_detect_content_crop_declines_on_a_full_frame_recording(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_color_video(popup, "yellow", 1.2)

    # Nothing to trim: no crop at all rather than a bogus one.
    assert detect_content_crop(popup) is None


def test_detect_content_crop_declines_without_a_stable_rect(tmp_path: Path) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_shifting_filler(popup, 1.5)

    # Taking the first frame's answer here would make the framed window jump.
    assert detect_content_crop(popup) is None


def _make_full_bleed_popup_with_ink(path: Path, seconds: float) -> None:
    """Write a 320x240 popup that fills its canvas, with darker ink inside it.

    The real shape of a featureless ``window.open``: no padding anywhere, the
    page's own background in every corner, and content painted on top.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=yellow:duration={seconds}:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:duration={seconds}:size=120x60:rate=25",
            "-filter_complex",
            "[0:v][1:v]overlay=x=40:y=50,format=yuv420p[outv]",
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


def test_detect_content_crop_declines_on_ink_inside_a_full_bleed_page(tmp_path: Path) -> None:
    """Ink floating inside a full-bleed page is not a window inside padding.

    Regression from a real recording: a featureless ``window.open`` filled the
    whole canvas, so the corner pixel sampled the *page's* background and the
    detection happily "trimmed" everything except the text. Playwright always
    anchors the popup at the top-left, so a rect that does not start at the
    origin is proof the reading is bogus.
    """
    popup = tmp_path / "popup.mp4"
    _make_full_bleed_popup_with_ink(popup, 1.2)

    assert detect_content_crop(popup) is None


def test_detect_content_crop_declines_on_a_missing_file(tmp_path: Path) -> None:
    # A last-resort heuristic must never abort a render that would otherwise
    # simply not crop.
    assert detect_content_crop(tmp_path / "absent.mp4") is None


def test_detect_content_crop_declines_when_ffmpeg_overruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.0)

    def timing_out(cmd, **kwargs):
        assert kwargs.get("timeout") == mux_module.CROPDETECT_TIMEOUT, (
            "every detection pass must carry the timeout"
        )
        raise subprocess.TimeoutExpired(cmd, mux_module.CROPDETECT_TIMEOUT)

    monkeypatch.setattr(mux_module, "_run", timing_out)

    # A wedged ffmpeg costs the crop, never the render.
    assert detect_content_crop(popup) is None


def test_detect_content_crop_passes_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popup = tmp_path / "popup.mp4"
    _make_popup_with_filler(popup, 1.0)
    timeouts: list[float | None] = []
    real_run = mux_module._run

    def spy_run(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mux_module, "_run", spy_run)

    assert detect_content_crop(popup) == (160, 120, 0, 0)
    # Both the padding sample and the cropdetect pass, not just one of them.
    assert timeouts and all(value == mux_module.CROPDETECT_TIMEOUT for value in timeouts), timeouts


def test_detect_content_crop_result_feeds_compose_popup_video(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_popup_with_filler(popup, 1.0)

    crop = detect_content_crop(popup)
    assert crop is not None
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, popup_crop=crop
    )

    # Same framing as the deterministic level-1 crop: filler gone, backdrop kept.
    _assert_yellow(_sample_region_rgb(out, 1.5, _CROPPED_RIGHT))
    _assert_dimmed_green(_sample_region_rgb(out, 1.5, _BORDER))


def test_compose_popup_video_explicit_transition_overrides_floating(tmp_path: Path) -> None:
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    # An explicit transition wins over the deprecated floating alias.
    compose_popup_video(
        main, popup, out, opened_at=1.0, closed_at=2.0, floating=True, transition="cut"
    )

    # Hard cut: the border is full popup yellow, not the dimmed backdrop float draws.
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
