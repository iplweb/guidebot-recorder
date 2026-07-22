"""Golden argv: every ``mux_audio_tracks`` command line, pinned element for element.

``mux_audio_tracks`` builds no filtergraph — it builds an argv, and for ffmpeg an
argv's *order* is behaviour, not formatting:

* ``-map`` selects streams and must precede the codec selection it feeds;
* ``-vf`` forces the encode, so it has to land before ``-c:v``, and a ``-c:v copy``
  emitted alongside it is a hard error rather than a lost fade;
* ``-metadata:s:a:N`` / ``-disposition:a:N`` attach to the *output* stream N, which
  only exists once the audio codec has been chosen;
* ``-t`` last is what keeps a malformed short bed from truncating the film.

Reordering any of those produces a command that still runs and a file that still
plays. The rest of the suite would not notice: it probes the result for languages,
dispositions and a codec name, all of which survive a reorder that silently drops
the fade or clamps to the wrong clock. So this module compares the whole list.

Paths and the ffmpeg binary are substituted for ``<name>`` placeholders (see
:func:`_placeheld`) — everything else is literal, including the codec block that
looks like boilerplate and is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import FadeSpec, MuxAudioTrack, ffmpeg_bin, mux_audio_tracks
from tests.unit.video._mux_helpers import (
    FFMPEG,
    _make_audio,
    _make_color_video,
    capture_ffmpeg_args,
)

pytestmark = FFMPEG

#: The video and the two beds every case below draws from. 2s at 25fps, and beds
#: of exactly the same length so the duration guard passes without rounding luck.
_SECONDS = 2.0


@dataclass(frozen=True)
class _Case:
    """One call to :func:`mux_audio_tracks` and the argv it must emit."""

    languages: tuple[str, ...]
    """Track keys, in order; the first is the default stream."""
    kwargs: dict[str, object] = field(default_factory=dict)
    argv: tuple[str, ...] = ()


_AUDIO_CODEC = (
    "-c:a",
    "aac",
    "-profile:a",
    "aac_low",
    "-b:a",
    "192k",
    "-ar",
    "48000",
    "-ac",
    "2",
)

CASES: dict[str, _Case] = {
    # The plain path: Playwright's picture is encoded, one bed, one default stream.
    "encode_single_track": _Case(
        languages=("pol",),
        kwargs={"video_duration": _SECONDS},
        argv=(
            "<ffmpeg>",
            "-y",
            "-i",
            "<video>",
            "-i",
            "<pol>",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            *_AUDIO_CODEC,
            "-metadata:s:a:0",
            "language=pol",
            "-metadata:s:a:0",
            "title=Polski",
            "-metadata:s:a:0",
            "handler_name=Polski",
            "-disposition:a:0",
            "default",
            "-movflags",
            "+faststart",
            "-t",
            "2.000000",
        ),
    ),
    # preencoded: the composited picture is copied. Two beds, so the metadata block
    # repeats per output stream and only the first carries `default`.
    "preencoded_two_tracks": _Case(
        languages=("pol", "eng"),
        kwargs={"preencoded": True, "video_duration": _SECONDS},
        argv=(
            "<ffmpeg>",
            "-y",
            "-i",
            "<video>",
            "-i",
            "<pol>",
            "-i",
            "<eng>",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:a:0",
            "-c:v",
            "copy",
            *_AUDIO_CODEC,
            "-metadata:s:a:0",
            "language=pol",
            "-metadata:s:a:0",
            "title=Polski",
            "-metadata:s:a:0",
            "handler_name=Polski",
            "-disposition:a:0",
            "default",
            "-metadata:s:a:1",
            "language=eng",
            "-metadata:s:a:1",
            "title=eng",
            "-metadata:s:a:1",
            "handler_name=eng",
            "-disposition:a:1",
            "0",
            "-movflags",
            "+faststart",
            "-t",
            "2.000000",
        ),
    ),
    # A fade cannot be applied to a copied stream, so it overrides `preencoded`:
    # `-c:v copy` is gone and every bed gets its own `-filter:a:N` in step.
    "fade_overrides_preencoded": _Case(
        languages=("pol", "eng"),
        kwargs={
            "preencoded": True,
            "video_duration": _SECONDS,
            "fade": FadeSpec(fade_in=0.5, fade_out=0.5),
        },
        argv=(
            "<ffmpeg>",
            "-y",
            "-i",
            "<video>",
            "-i",
            "<pol>",
            "-i",
            "<eng>",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:a:0",
            "-vf",
            "fade=t=in:st=0:d=0.500000:c=black,fade=t=out:st=1.500000:d=0.500000:c=black",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-filter:a:0",
            "afade=t=in:st=0:d=0.500000,afade=t=out:st=1.500000:d=0.500000",
            "-filter:a:1",
            "afade=t=in:st=0:d=0.500000,afade=t=out:st=1.500000:d=0.500000",
            *_AUDIO_CODEC,
            "-metadata:s:a:0",
            "language=pol",
            "-metadata:s:a:0",
            "title=Polski",
            "-metadata:s:a:0",
            "handler_name=Polski",
            "-disposition:a:0",
            "default",
            "-metadata:s:a:1",
            "language=eng",
            "-metadata:s:a:1",
            "title=eng",
            "-metadata:s:a:1",
            "handler_name=eng",
            "-disposition:a:1",
            "0",
            "-movflags",
            "+faststart",
            "-t",
            "2.000000",
        ),
    ),
    # `audio=False` fades the picture only: the `-vf` stays, every `-filter:a:N` goes.
    "fade_picture_only": _Case(
        languages=("pol",),
        kwargs={"video_duration": _SECONDS, "fade": FadeSpec(fade_in=0.5, audio=False)},
        argv=(
            "<ffmpeg>",
            "-y",
            "-i",
            "<video>",
            "-i",
            "<pol>",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            "fade=t=in:st=0:d=0.500000:c=black",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            *_AUDIO_CODEC,
            "-metadata:s:a:0",
            "language=pol",
            "-metadata:s:a:0",
            "title=Polski",
            "-metadata:s:a:0",
            "handler_name=Polski",
            "-disposition:a:0",
            "default",
            "-movflags",
            "+faststart",
            "-t",
            "2.000000",
        ),
    ),
}


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The picture and the two beds, built once for the whole module."""
    directory = tmp_path_factory.mktemp("mux_argv")
    paths = {
        "video": directory / "video.mp4",
        "pol": directory / "pol.wav",
        "eng": directory / "eng.wav",
    }
    _make_color_video(paths["video"], "white", _SECONDS)
    _make_audio(paths["pol"], _SECONDS)
    _make_audio(paths["eng"], _SECONDS)
    return paths


#: Titles are part of the argv, so they are fixed here rather than left to a
#: default: ``pol`` carries one, ``eng`` deliberately does not (its title falls
#: back to the language code, which the golden argv records).
_TITLES = {"pol": "Polski", "eng": None}


def _placeheld(cmd: list[str], sources: dict[str, Path]) -> list[str]:
    """Replace the ffmpeg binary and every input path with its ``<name>`` placeholder."""
    lookup = {str(path): f"<{name}>" for name, path in sources.items()}
    lookup[ffmpeg_bin()] = "<ffmpeg>"
    return [lookup.get(arg, arg) for arg in cmd]


@pytest.mark.parametrize("case_id", list(CASES))
def test_mux_audio_tracks_emits_the_recorded_argv(
    case_id: str,
    sources: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CASES[case_id]
    tracks = [
        MuxAudioTrack(
            path=sources[language],
            language=language,
            title=_TITLES[language],
            default=index == 0,
        )
        for index, language in enumerate(case.languages)
    ]
    seen = capture_ffmpeg_args(monkeypatch)

    mux_audio_tracks(sources["video"], tracks, tmp_path / "out.mp4", **case.kwargs)

    assert len(seen) == 1
    assert _placeheld(seen[0], sources) == list(case.argv)
