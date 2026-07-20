"""Pure time-model tests — no ffmpeg, no I/O."""

from __future__ import annotations

import json

import pytest

from guidebot_recorder.video.timeline import (
    FPS,
    TimeEdit,
    Timeline,
    TimelineError,
    build_filtergraph,
    frames_to_seconds,
    seconds_to_frames,
)


def test_fps_is_twenty_five() -> None:
    assert FPS == 25


def test_seconds_to_frames_rounds_to_nearest() -> None:
    assert seconds_to_frames(0.0) == 0
    assert seconds_to_frames(1.0) == 25
    assert seconds_to_frames(2.36) == 59
    # 2.37 * 25 == 59.25 -> nearest whole frame is 59
    assert seconds_to_frames(2.37) == 59
    # 2.39 * 25 == 59.75 -> nearest whole frame is 60
    assert seconds_to_frames(2.39) == 60


def test_frames_to_seconds_is_exact_on_the_grid() -> None:
    assert frames_to_seconds(0) == 0.0
    assert frames_to_seconds(25) == 1.0
    assert frames_to_seconds(59) == 2.36


def test_empty_timeline_is_identity() -> None:
    tl = Timeline.build([], source_frames=148)
    assert tl.is_empty
    assert tl.to_virtual(0) == 0
    assert tl.to_virtual(75) == 75
    assert tl.virtual_frames == 148


def test_freeze_shifts_only_later_frames() -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=148)
    assert tl.to_virtual(0) == 0
    assert tl.to_virtual(74) == 74
    # A timestamp exactly at the freeze point maps to the START of the hold:
    # narration begins there, the picture stops there.
    assert tl.to_virtual(75) == 75
    assert tl.to_virtual(76) == 76 + 59
    assert tl.virtual_frames == 148 + 59


def test_cut_pulls_later_frames_back() -> None:
    tl = Timeline.build([TimeEdit(at=25, kind="cut", frames=25)], source_frames=148)
    assert tl.to_virtual(0) == 0
    assert tl.to_virtual(25) == 25
    assert tl.to_virtual(50) == 25
    assert tl.to_virtual(100) == 75
    assert tl.virtual_frames == 148 - 25


def test_timestamp_inside_a_cut_clamps_to_its_start() -> None:
    tl = Timeline.build([TimeEdit(at=25, kind="cut", frames=25)], source_frames=148)
    # Frames 25..49 are removed; anything landing there clamps to the cut start.
    assert tl.to_virtual(30) == 25
    assert tl.to_virtual(49) == 25


def test_interleaved_freeze_and_cut() -> None:
    tl = Timeline.build(
        [
            TimeEdit(at=25, kind="cut", frames=25),
            TimeEdit(at=75, kind="freeze", frames=59),
        ],
        source_frames=148,
    )
    assert tl.to_virtual(75) == 75 - 25
    assert tl.to_virtual(76) == 76 - 25 + 59
    assert tl.virtual_frames == 148 - 25 + 59


def test_edits_are_sorted_regardless_of_input_order() -> None:
    unsorted = [
        TimeEdit(at=75, kind="freeze", frames=10),
        TimeEdit(at=25, kind="freeze", frames=10),
    ]
    tl = Timeline.build(unsorted, source_frames=148)
    assert [e.at for e in tl.edits] == [25, 75]
    assert tl.to_virtual(100) == 120


def test_five_freezes_accumulate_exactly() -> None:
    """The regression that would otherwise trip the 0.05s guard in mux.py."""
    edits = [TimeEdit(at=10 * (i + 1), kind="freeze", frames=59) for i in range(5)]
    tl = Timeline.build(edits, source_frames=148)
    assert tl.virtual_frames == 148 + 5 * 59
    # Exact on the grid: no float drift anywhere in the model.
    assert tl.virtual_duration == pytest.approx((148 + 295) / 25, abs=0.0)


def test_to_virtual_seconds_converts_at_the_boundary() -> None:
    tl = Timeline.build([TimeEdit(at=25, kind="freeze", frames=25)], source_frames=148)
    # 2.0s -> frame 50 -> virtual frame 75 -> 3.0s
    assert tl.to_virtual_seconds(2.0) == pytest.approx(3.0)


@pytest.mark.parametrize(
    "edit",
    [
        TimeEdit(at=-1, kind="freeze", frames=10),
        TimeEdit(at=10, kind="freeze", frames=0),
        TimeEdit(at=10, kind="freeze", frames=-5),
    ],
)
def test_rejects_malformed_edits(edit: TimeEdit) -> None:
    with pytest.raises(TimelineError):
        Timeline.build([edit], source_frames=148)


def test_rejects_edit_beyond_the_recording() -> None:
    with pytest.raises(TimelineError):
        Timeline.build([TimeEdit(at=200, kind="freeze", frames=10)], source_frames=148)
    with pytest.raises(TimelineError):
        Timeline.build([TimeEdit(at=140, kind="cut", frames=20)], source_frames=148)


def test_rejects_two_edits_at_the_same_frame() -> None:
    with pytest.raises(TimelineError):
        Timeline.build(
            [
                TimeEdit(at=50, kind="freeze", frames=10),
                TimeEdit(at=50, kind="freeze", frames=10),
            ],
            source_frames=148,
        )


def test_rejects_edit_landing_inside_a_cut() -> None:
    with pytest.raises(TimelineError):
        Timeline.build(
            [
                TimeEdit(at=25, kind="cut", frames=50),
                TimeEdit(at=40, kind="freeze", frames=10),
            ],
            source_frames=148,
        )


def test_rejects_non_positive_source_frames() -> None:
    with pytest.raises(TimelineError):
        Timeline.build([], source_frames=0)


def test_filtergraph_for_a_single_freeze() -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=2[s0][s1];"
        "[s0]trim=start_frame=0:end_frame=76,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=59[v0];"
        "[s1]trim=start_frame=76,setpts=PTS-STARTPTS[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[v]"
    )


def test_filtergraph_for_a_single_cut() -> None:
    tl = Timeline.build([TimeEdit(at=25, kind="cut", frames=25)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=2[s0][s1];"
        "[s0]trim=start_frame=0:end_frame=25,setpts=PTS-STARTPTS[v0];"
        "[s1]trim=start_frame=50,setpts=PTS-STARTPTS[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[v]"
    )


def test_filtergraph_for_cut_then_freeze() -> None:
    tl = Timeline.build(
        [
            TimeEdit(at=25, kind="cut", frames=25),
            TimeEdit(at=75, kind="freeze", frames=59),
        ],
        source_frames=148,
    )
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=3[s0][s1][s2];"
        "[s0]trim=start_frame=0:end_frame=25,setpts=PTS-STARTPTS[v0];"
        "[s1]trim=start_frame=50:end_frame=76,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=59[v1];"
        "[s2]trim=start_frame=76,setpts=PTS-STARTPTS[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
    )


def test_filtergraph_rejects_an_empty_timeline() -> None:
    tl = Timeline.build([], source_frames=148)
    with pytest.raises(TimelineError):
        build_filtergraph(tl)


def test_filtergraph_handles_a_freeze_on_the_last_frame() -> None:
    tl = Timeline.build([TimeEdit(at=147, kind="freeze", frames=25)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=1[s0];"
        "[s0]trim=start_frame=0,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=25[v0];"
        "[v0]concat=n=1:v=1:a=0[v]"
    )


def test_filtergraph_never_emits_a_single_frame_segment_between_freezes() -> None:
    """Freezes two frames apart must not leave a one-frame segment behind.

    ``concat`` measures an input's frame duration from the frames it receives,
    so a one-frame input measures as zero-length and the next segment lands on
    top of it. Folding each freeze into the run it terminates keeps every
    segment at least two frames long.
    """
    tl = Timeline.build(
        [TimeEdit(at=at, kind="freeze", frames=2) for at in (2, 4, 7)],
        source_frames=148,
    )
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=4[s0][s1][s2][s3];"
        "[s0]trim=start_frame=0:end_frame=3,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=2[v0];"
        "[s1]trim=start_frame=3:end_frame=5,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=2[v1];"
        "[s2]trim=start_frame=5:end_frame=8,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=2[v2];"
        "[s3]trim=start_frame=8,setpts=PTS-STARTPTS[v3];"
        "[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]"
    )


def test_filtergraph_rejects_a_lone_kept_frame_before_a_cut() -> None:
    """The one shape freezes cannot produce, and concat cannot render."""
    tl = Timeline.build(
        [TimeEdit(at=0, kind="freeze", frames=2), TimeEdit(at=2, kind="cut", frames=5)],
        source_frames=148,
    )
    with pytest.raises(TimelineError, match="single frame"):
        build_filtergraph(tl)


def test_filtergraph_allows_a_lone_kept_frame_as_the_final_segment() -> None:
    """Nothing is concatenated after the last segment, so its length is free."""
    tl = Timeline.build([TimeEdit(at=146, kind="freeze", frames=2)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=2[s0][s1];"
        "[s0]trim=start_frame=0:end_frame=147,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=2[v0];"
        "[s1]trim=start_frame=147,setpts=PTS-STARTPTS[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[v]"
    )


def test_to_json_describes_both_axes() -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=148)
    payload = json.loads(tl.to_json())
    assert payload["fps"] == 25
    assert payload["source_frames"] == 148
    assert payload["virtual_frames"] == 207
    assert payload["virtual_duration"] == pytest.approx(8.28)
    assert payload["edits"] == [{"at": 75, "kind": "freeze", "frames": 59}]
