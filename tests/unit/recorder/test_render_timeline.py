"""``recorder.render.timeline``: ``_build_timeline`` freeze merging and edit application.

Split out of the original ``test_render.py``.
"""

import pytest

from guidebot_recorder.recorder.render import RenderError, _build_timeline
from guidebot_recorder.recorder.render.timeline import _apply_timeline_edits
from guidebot_recorder.video.timeline import TimeEdit, Timeline

from ._render_helpers import FFMPEG

pytestmark = FFMPEG


def test_build_timeline_merges_two_freezes_on_the_same_frame() -> None:
    # Two edits landing on the same frame index both want the picture held
    # there, so the film holds it for the total of both. `_build_timeline`
    # takes raw `TimeEdit`s directly, bypassing `Config` and its
    # `hold_frame_settle` floor, so this stays exercised regardless of what
    # settle values `Config` accepts.
    timeline = _build_timeline(
        [
            TimeEdit(at=40, kind="freeze", frames=25),
            TimeEdit(at=40, kind="freeze", frames=50),
        ],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=40, kind="freeze", frames=75),)
    assert timeline.virtual_frames == 100 + 75


def test_build_timeline_merges_three_freezes_on_the_same_frame() -> None:
    timeline = _build_timeline(
        [TimeEdit(at=7, kind="freeze", frames=n) for n in (10, 20, 30)],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=7, kind="freeze", frames=60),)
    assert timeline.virtual_frames == 100 + 60


def test_build_timeline_clamps_a_freeze_past_the_end() -> None:
    timeline = _build_timeline(
        [TimeEdit(at=140, kind="freeze", frames=30)],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=99, kind="freeze", frames=30),)
    assert timeline.virtual_frames == 100 + 30


def test_build_timeline_clamps_a_freeze_exactly_at_source_frames_and_merges() -> None:
    # The postroll is only 0.1s, so a freeze stamped at the very end rounds onto
    # (or past) the last frame. Clamping happens before merging, so a clamped
    # edit coalesces with one already sitting on the last frame.
    timeline = _build_timeline(
        [
            TimeEdit(at=99, kind="freeze", frames=12),
            TimeEdit(at=100, kind="freeze", frames=13),
        ],
        source_frames=100,
    )
    assert timeline.edits == (TimeEdit(at=99, kind="freeze", frames=25),)
    assert timeline.virtual_frames == 100 + 25


def test_build_timeline_keeps_distinct_frames_apart() -> None:
    timeline = _build_timeline(
        [
            TimeEdit(at=10, kind="freeze", frames=5),
            TimeEdit(at=20, kind="freeze", frames=7),
        ],
        source_frames=100,
    )
    assert timeline.edits == (
        TimeEdit(at=10, kind="freeze", frames=5),
        TimeEdit(at=20, kind="freeze", frames=7),
    )
    assert timeline.virtual_frames == 100 + 12


def test_apply_timeline_edits_rejects_a_file_that_disagrees_with_the_model(
    tmp_path, monkeypatch
) -> None:
    # Nothing downstream re-probes the video: `mux_audio_tracks` is handed the
    # model's own duration, so its tolerance compares the model against itself.
    # This is the only place the model meets reality.
    import guidebot_recorder.recorder.render as R

    timeline = Timeline.build([TimeEdit(at=10, kind="freeze", frames=25)], source_frames=100)
    monkeypatch.setattr(R.timeline, "apply_time_edits", lambda src, tl, out: None)
    # `probe_frame_count` has a second consumer in `post` (it sizes the timeline
    # before the edit runs), so replacing it takes both lines.
    monkeypatch.setattr(R.timeline, "probe_frame_count", lambda path: 123)
    monkeypatch.setattr(R.post, "probe_frame_count", lambda path: 123)

    with pytest.raises(RenderError) as excinfo:
        _apply_timeline_edits(tmp_path / "src.mp4", timeline, tmp_path / "out.mp4")

    message = str(excinfo.value)
    assert "123" in message
    assert str(timeline.virtual_frames) in message
