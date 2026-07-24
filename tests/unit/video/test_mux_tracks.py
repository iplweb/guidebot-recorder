"""ffmpeg-backed tests for ``video.mux.tracks`` — muxing audio onto the picture.

Covers ``mux``, ``mux_audio_tracks`` (language/title/disposition metadata,
validation, ``preencoded`` copy, duration reuse, fades) and ``mux_preencoded``,
plus ``FadeSpec.is_noop``. These judge the rendered *result* — stream layout,
codec, tags, sampled brightness; the exact argv each call emits is pinned by
string equality in ``test_mux_argv.py``.

Input material is generated with ffmpeg's lavfi sources, so the tests need no
fixtures on disk; they are skipped when ffmpeg/ffprobe are not installed. No
shared conftest by design — the shared builders, samplers and the marker block
come from the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import (
    FadeSpec,
    MuxAudioTrack,
    mux,
    mux_audio_tracks,
    mux_preencoded,
)
from guidebot_recorder.video.mux.probe import probe_duration
from tests.unit.video._mux_helpers import (
    FFMPEG,
    _make_audio,
    _make_color_video,
    _make_video,
    _sample_rgb,
    _video_codec,
)

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


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
    original_probe = mux_module.probe.probe_duration

    def recording_probe(path: Path) -> float:
        probed.append(Path(path))
        return original_probe(path)

    monkeypatch.setattr(mux_module.probe, "probe_duration", recording_probe)

    mux_audio_tracks(
        video,
        [MuxAudioTrack(audio, language="pol", default=True)],
        out,
        video_duration=1.0,
    )

    assert probed == [audio]
    assert out.exists()


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


def _fade_track(path: Path, seconds: float) -> MuxAudioTrack:
    _make_audio(path, seconds)
    return MuxAudioTrack(path=path, language="pol", title="Polski", default=True)


def test_mux_audio_tracks_fades_the_picture_from_and_to_black(tmp_path):
    video = tmp_path / "video.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(video, "white", 3.0)
    track = _fade_track(tmp_path / "pol.wav", 3.0)

    mux_audio_tracks(
        video, [track], out, video_duration=3.0, fade=FadeSpec(fade_in=0.5, fade_out=0.5)
    )

    # Ends ramp from/to black; the middle keeps the source picture untouched.
    # Asserted by brightness rather than an exact colour: where on the ramp a
    # given timestamp lands depends on the frame grid, the direction does not.
    assert max(_sample_rgb(out, 0.02)) < 60
    assert _sample_rgb(out, 1.5) == pytest.approx((255, 255, 255), abs=20)
    assert max(_sample_rgb(out, 2.9)) < 90
    assert probe_duration(out) == pytest.approx(3.0, abs=0.1)


def test_mux_audio_tracks_fades_to_a_configured_colour(tmp_path):
    video = tmp_path / "video.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(video, "white", 2.0)
    track = _fade_track(tmp_path / "pol.wav", 2.0)

    mux_audio_tracks(
        video, [track], out, video_duration=2.0, fade=FadeSpec(fade_in=0.5, color="blue")
    )

    red, green, blue = _sample_rgb(out, 0.02)
    assert blue > 150 and red < 80 and green < 80


def test_mux_audio_tracks_fade_overrides_stream_copy(tmp_path):
    # A filtered stream cannot also be copied: the fade must win over
    # ``preencoded`` rather than being silently dropped.
    video = tmp_path / "video.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(video, "white", 2.0)
    track = _fade_track(tmp_path / "pol.wav", 2.0)

    mux_audio_tracks(
        video, [track], out, preencoded=True, video_duration=2.0, fade=FadeSpec(fade_in=0.5)
    )

    assert _video_codec(out) == "h264"
    assert max(_sample_rgb(out, 0.02)) < 60


def test_mux_audio_tracks_without_a_fade_still_copies(tmp_path):
    video = tmp_path / "video.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(video, "white", 2.0)
    track = _fade_track(tmp_path / "pol.wav", 2.0)

    mux_audio_tracks(video, [track], out, preencoded=True, video_duration=2.0, fade=None)

    assert _sample_rgb(out, 0.02) == pytest.approx((255, 255, 255), abs=20)


def test_mux_audio_tracks_rejects_a_fade_longer_than_the_film(tmp_path):
    video = tmp_path / "video.mp4"
    out = tmp_path / "out.mp4"
    _make_color_video(video, "white", 1.0)
    track = _fade_track(tmp_path / "pol.wav", 1.0)

    with pytest.raises(ValueError, match="longer than the film"):
        mux_audio_tracks(
            video, [track], out, video_duration=1.0, fade=FadeSpec(fade_in=0.8, fade_out=0.8)
        )


def test_fade_spec_noop_is_recognised():
    assert FadeSpec().is_noop()
    assert not FadeSpec(fade_in=0.4).is_noop()
    assert not FadeSpec(fade_out=0.4).is_noop()
