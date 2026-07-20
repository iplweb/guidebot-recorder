# Hold Frame For Narration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop paying narration duration in recording wall-clock time by holding a still frame in post-production instead of sleeping through the voice-over.

**Architecture:** A new pure module `guidebot_recorder/video/timeline.py` models render time as integer frames at 25 fps and exposes one mapping, `to_virtual`, that every post-recording timestamp passes through. Render collects `TimeEdit` records instead of sleeping, then a new ffmpeg stage — placed after popup composition and before audio mux — rewrites the video with `trim`/`tpad`/`concat`. Audio offsets are derived from the same integer frame counts, so the audio and video axes agree to 0 ms.

**Tech Stack:** Python 3.12+, Pydantic v2, Playwright, ffmpeg/ffprobe, pytest (`asyncio_mode = "auto"`), Typer CLI, ruff (line-length 100).

## Global Constraints

- `FPS = 25` — a hardcoded constant in Playwright's video recorder. Assert it from the probe; never adapt to a different value.
- Time is modelled in **integer frames end to end**. Seconds appear only at boundaries (`round(t * FPS)` in, `frames / FPS` out).
- ffmpeg receives frame counts (`tpad=stop=N`, `trim=start_frame/end_frame`), never float durations.
- Popup `opened_at`/`closed_at` stay on the recording axis. Only narration and SFX offsets are remapped.
- Total virtual time per narrated step stays exactly `D` (narration length), so finished-film pacing is unchanged.
- ruff line-length is 100. Run `uv run ruff check .` and `uv run ruff format .` before every commit.
- Tests requiring ffmpeg get `@pytest.mark.ffmpeg`. Full-pipeline tests get `@pytest.mark.integration`.
- Spec: `docs/superpowers/specs/2026-07-20-hold-frame-for-narration-design.md`.

---

## File Structure

| File | Responsibility |
|---|---|
| `guidebot_recorder/video/timeline.py` (create) | Time model + filtergraph + ffmpeg stage. Self-contained; no imports from `recorder/`. |
| `tests/unit/video/test_timeline.py` (create) | Pure model tests (no ffmpeg) + filtergraph golden strings. |
| `tests/unit/video/test_timeline_ffmpeg.py` (create) | Real ffmpeg round-trips, `@pytest.mark.ffmpeg`. |
| `guidebot_recorder/models/config.py` (modify) | Two new `Config` fields. |
| `guidebot_recorder/cli.py` (modify) | `--no-hold-frame`, `--hold-frame-settle`, `--dump-timeline`. |
| `guidebot_recorder/recorder/render.py` (modify) | Collect edits instead of sleeping; remap offsets; insert stage. |
| `docs/en/scenario-reference.md`, `docs/pl/scenario-reference.md` (modify) | Document the config fields. |
| `docs/en/cli-reference.md`, `docs/pl/cli-reference.md` (modify) | Document the flags. |

---

### Task 1: Time model

Pure functions and dataclasses. No ffmpeg, no I/O, no browser. This is where the correctness of the whole feature lives.

**Files:**
- Create: `guidebot_recorder/video/timeline.py`
- Test: `tests/unit/video/test_timeline.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `FPS: int`, `seconds_to_frames(float) -> int`, `frames_to_seconds(int) -> float`, `TimeEdit(at: int, kind: Literal["freeze","cut"], frames: int)`, `Timeline.build(edits: Iterable[TimeEdit], source_frames: int) -> Timeline`, `Timeline.to_virtual(int) -> int`, `Timeline.to_virtual_seconds(float) -> float`, `Timeline.virtual_frames -> int`, `Timeline.virtual_duration -> float`, `Timeline.is_empty -> bool`, `TimelineError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/video/test_timeline.py`:

```python
"""Pure time-model tests — no ffmpeg, no I/O."""

from __future__ import annotations

import pytest

from guidebot_recorder.video.timeline import (
    FPS,
    TimeEdit,
    Timeline,
    TimelineError,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/video/test_timeline.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'guidebot_recorder.video.timeline'`

- [ ] **Step 3: Write the implementation**

Create `guidebot_recorder/video/timeline.py`:

```python
"""Render time as data: an explicit, frame-exact map from recording to film.

Playwright records at a fixed 25 fps, so the whole model is expressed in whole
frames. Seconds only appear at the boundaries — wall-clock readings convert in,
audio offsets convert out. Nothing in between is a float, because ``tpad``
quantises to the frame grid and any float slack accumulates across freezes until
it trips the duration guard in :mod:`guidebot_recorder.video.mux`.

Two axes exist:

``t_real``
    the recording produced by Playwright.
``t_virtual``
    the finished film, after freezes are inserted and cuts removed.

:meth:`Timeline.to_virtual` is the only bridge between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

FPS = 25
"""Frames per second of a Playwright screencast.

Hardcoded in Playwright's ``videoRecorder`` and not exposed through the API.
Verified: every inter-frame delta is exactly 0.04 s, even when the page's main
thread stalls (Chromium repeats the last frame rather than dropping the grid).
"""

EditKind = Literal["freeze", "cut"]


class TimelineError(ValueError):
    """A timeline that cannot be rendered — raised before ffmpeg is invoked."""


def seconds_to_frames(seconds: float) -> int:
    """Convert *seconds* to the nearest whole frame."""
    return int(round(seconds * FPS))


def frames_to_seconds(frames: int) -> float:
    """Convert a whole number of *frames* to seconds, exactly on the grid."""
    return frames / FPS


@dataclass(frozen=True)
class TimeEdit:
    """One edit applied to the recording axis.

    ``freeze``
        hold the frame at index *at* for *frames* additional frames.
    ``cut``
        drop frames ``[at, at + frames)`` from the recording.
    """

    at: int
    kind: EditKind
    frames: int


@dataclass(frozen=True)
class Timeline:
    """An ordered, validated set of edits over a recording of known length."""

    edits: tuple[TimeEdit, ...]
    source_frames: int

    @classmethod
    def build(cls, edits: Iterable[TimeEdit], source_frames: int) -> Timeline:
        """Validate and order *edits*, failing loud on anything unrenderable."""
        if source_frames <= 0:
            raise TimelineError(f"source_frames must be positive, got {source_frames}")

        ordered = tuple(sorted(edits, key=lambda e: e.at))

        for edit in ordered:
            if edit.at < 0:
                raise TimelineError(f"edit position must be >= 0, got {edit.at}")
            if edit.frames <= 0:
                raise TimelineError(f"edit length must be positive, got {edit.frames}")
            if edit.at >= source_frames:
                raise TimelineError(
                    f"edit at frame {edit.at} is beyond the recording ({source_frames} frames)"
                )
            if edit.kind == "cut" and edit.at + edit.frames > source_frames:
                raise TimelineError(
                    f"cut [{edit.at}, {edit.at + edit.frames}) overruns "
                    f"the recording ({source_frames} frames)"
                )

        for previous, current in zip(ordered, ordered[1:]):
            if current.at == previous.at:
                raise TimelineError(f"two edits share frame {current.at}")
            if previous.kind == "cut" and current.at < previous.at + previous.frames:
                raise TimelineError(
                    f"edit at frame {current.at} falls inside the cut "
                    f"[{previous.at}, {previous.at + previous.frames})"
                )

        return cls(edits=ordered, source_frames=source_frames)

    @property
    def is_empty(self) -> bool:
        """Whether this timeline leaves the recording untouched."""
        return not self.edits

    def to_virtual(self, frame: int) -> int:
        """Map a recording frame index onto the finished film.

        A frame inside a cut span clamps to that span's start: cuts remove dead
        time, so nothing meaningful is lost, but the choice is deliberate rather
        than an accident of arithmetic.
        """
        shift = 0
        for edit in self.edits:
            if edit.kind == "freeze":
                if edit.at < frame:
                    shift += edit.frames
            else:
                if edit.at + edit.frames <= frame:
                    shift -= edit.frames
                elif edit.at < frame:
                    return edit.at + shift
        return frame + shift

    def to_virtual_seconds(self, t_real: float) -> float:
        """Map a wall-clock offset (seconds) onto the finished film."""
        return frames_to_seconds(self.to_virtual(seconds_to_frames(t_real)))

    @property
    def virtual_frames(self) -> int:
        """Length of the finished film, in frames."""
        return self.to_virtual(self.source_frames)

    @property
    def virtual_duration(self) -> float:
        """Length of the finished film, in seconds."""
        return frames_to_seconds(self.virtual_frames)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/video/test_timeline.py -v`
Expected: all PASS

- [ ] **Step 5: Lint**

Run: `uv run ruff format guidebot_recorder/video/timeline.py tests/unit/video/test_timeline.py && uv run ruff check guidebot_recorder/video/timeline.py tests/unit/video/test_timeline.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add guidebot_recorder/video/timeline.py tests/unit/video/test_timeline.py
git commit -m "feat(timeline): frame-exact time model for freeze and cut edits"
```

---

### Task 2: Filtergraph and the ffmpeg stage

Turns a `Timeline` into video. The filtergraph shape here was verified empirically — do not "simplify" it to float `trim=start=`/`stop_duration=`; that reintroduces the drift Task 1 exists to prevent.

**Files:**
- Modify: `guidebot_recorder/video/timeline.py`
- Test: `tests/unit/video/test_timeline.py` (filtergraph strings), `tests/unit/video/test_timeline_ffmpeg.py` (create)

**Interfaces:**
- Consumes: `Timeline`, `TimeEdit`, `FPS`, `TimelineError` from Task 1; `ffmpeg_bin`, `ffprobe_bin`, `_run`, `_run_to_output`, `probe_duration` from `guidebot_recorder.video.mux`.
- Produces: `build_filtergraph(timeline: Timeline) -> str`, `apply_time_edits(src: Path, timeline: Timeline, out: Path) -> None`, `probe_frame_count(path: Path) -> int`, `assert_recording_fps(path: Path) -> None`.

- [ ] **Step 1: Write the failing filtergraph tests**

Append to `tests/unit/video/test_timeline.py`:

```python
from guidebot_recorder.video.timeline import build_filtergraph


def test_filtergraph_for_a_single_freeze() -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=3[s0][s1][s2];"
        "[s0]trim=start_frame=0:end_frame=75,setpts=PTS-STARTPTS[v0];"
        "[s1]trim=start_frame=75:end_frame=76,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=59[v1];"
        "[s2]trim=start_frame=76,setpts=PTS-STARTPTS[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
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
        "[0:v]fps=25,split=4[s0][s1][s2][s3];"
        "[s0]trim=start_frame=0:end_frame=25,setpts=PTS-STARTPTS[v0];"
        "[s1]trim=start_frame=50:end_frame=75,setpts=PTS-STARTPTS[v1];"
        "[s2]trim=start_frame=75:end_frame=76,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=59[v2];"
        "[s3]trim=start_frame=76,setpts=PTS-STARTPTS[v3];"
        "[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]"
    )


def test_filtergraph_rejects_an_empty_timeline() -> None:
    tl = Timeline.build([], source_frames=148)
    with pytest.raises(TimelineError):
        build_filtergraph(tl)


def test_filtergraph_handles_a_freeze_on_the_last_frame() -> None:
    tl = Timeline.build([TimeEdit(at=147, kind="freeze", frames=25)], source_frames=148)
    assert build_filtergraph(tl) == (
        "[0:v]fps=25,split=2[s0][s1];"
        "[s0]trim=start_frame=0:end_frame=147,setpts=PTS-STARTPTS[v0];"
        "[s1]trim=start_frame=147:end_frame=148,setpts=PTS-STARTPTS,"
        "tpad=stop_mode=clone:stop=25[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[v]"
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/video/test_timeline.py -k filtergraph -v`
Expected: FAIL — `ImportError: cannot import name 'build_filtergraph'`

- [ ] **Step 3: Implement the filtergraph builder**

Append to `guidebot_recorder/video/timeline.py` (add `from pathlib import Path` and the `mux` imports to the existing import block):

```python
def _segments(timeline: Timeline) -> list[tuple[str, int, int]]:
    """Decompose a timeline into ordered output segments.

    Yields ``("keep", start_frame, end_frame)`` and ``("freeze", at, frames)``.
    A freeze emits source frame ``[at, at+1)`` a total of ``frames + 1`` times
    and adds ``frames`` net; the following kept span therefore resumes at
    ``at + 1``.
    """
    out: list[tuple[str, int, int]] = []
    cursor = 0
    for edit in timeline.edits:
        if edit.at > cursor:
            out.append(("keep", cursor, edit.at))
        if edit.kind == "freeze":
            out.append(("freeze", edit.at, edit.frames))
            cursor = edit.at + 1
        else:
            cursor = edit.at + edit.frames
    if cursor < timeline.source_frames:
        out.append(("keep", cursor, timeline.source_frames))
    return out


def build_filtergraph(timeline: Timeline) -> str:
    """Build the ffmpeg ``-filter_complex`` graph realising *timeline*.

    Boundaries are frame indices, never float seconds: ``trim=start_frame`` and
    ``tpad=stop=N`` are exact on the frame grid, whereas their second-valued
    counterparts round to the nearest frame and accumulate error across freezes.

    The leading ``fps=25`` is a no-op on current Playwright output (verified
    frame-for-frame identical) and is kept only as a defensive normaliser.
    """
    if timeline.is_empty:
        raise TimelineError("cannot build a filtergraph for an empty timeline")

    segments = _segments(timeline)
    count = len(segments)
    splits = "".join(f"[s{i}]" for i in range(count))
    parts = [f"[0:v]fps={FPS},split={count}{splits}"]

    for i, (kind, a, b) in enumerate(segments):
        if kind == "keep":
            # The tail segment omits end_frame so it runs to the end of input.
            bounds = (
                f"trim=start_frame={a}:end_frame={b}"
                if b < timeline.source_frames
                else f"trim=start_frame={a}"
            )
            parts.append(f"[s{i}]{bounds},setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(
                f"[s{i}]trim=start_frame={a}:end_frame={a + 1},setpts=PTS-STARTPTS,"
                f"tpad=stop_mode=clone:stop={b}[v{i}]"
            )

    labels = "".join(f"[v{i}]" for i in range(count))
    parts.append(f"{labels}concat=n={count}:v=1:a=0[v]")
    return ";".join(parts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/video/test_timeline.py -k filtergraph -v`
Expected: all PASS

- [ ] **Step 5: Write the failing ffmpeg round-trip tests**

Create `tests/unit/video/test_timeline_ffmpeg.py`:

```python
"""Real ffmpeg round-trips for the time-edit stage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import ffmpeg_bin
from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    TimelineError,
    apply_time_edits,
    assert_recording_fps,
    probe_frame_count,
)

pytestmark = pytest.mark.ffmpeg

SOURCE_FRAMES = 148


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A 148-frame CFR-25 clip, standing in for a Playwright screencast."""
    out = tmp_path / "src.mp4"
    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=25:duration={SOURCE_FRAMES / 25}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "25",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def test_probe_frame_count_matches_the_source(source: Path) -> None:
    assert probe_frame_count(source) == SOURCE_FRAMES


def test_assert_recording_fps_accepts_twenty_five(source: Path) -> None:
    assert_recording_fps(source)  # does not raise


def test_single_freeze_is_frame_exact(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=SOURCE_FRAMES)
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES + 59


def test_three_freezes_do_not_accumulate_error(source: Path, tmp_path: Path) -> None:
    """The drift regression: float seconds would lose ~30ms here."""
    tl = Timeline.build(
        [TimeEdit(at=25 * (i + 1), kind="freeze", frames=59) for i in range(3)],
        source_frames=SOURCE_FRAMES,
    )
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES + 3 * 59


def test_cut_and_freeze_combined_is_frame_exact(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build(
        [
            TimeEdit(at=25, kind="cut", frames=25),
            TimeEdit(at=75, kind="freeze", frames=59),
        ],
        source_frames=SOURCE_FRAMES,
    )
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert probe_frame_count(out) == tl.virtual_frames == SOURCE_FRAMES - 25 + 59


def test_output_stays_cfr_twenty_five(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=SOURCE_FRAMES)
    out = tmp_path / "out.mp4"
    apply_time_edits(source, tl, out)
    assert_recording_fps(out)  # does not raise


def test_apply_rejects_a_timeline_longer_than_the_source(source: Path, tmp_path: Path) -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=999)
    with pytest.raises(TimelineError):
        apply_time_edits(source, tl, tmp_path / "out.mp4")
```

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/unit/video/test_timeline_ffmpeg.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_time_edits'`

- [ ] **Step 7: Implement the ffmpeg stage and probes**

Append to `guidebot_recorder/video/timeline.py`:

```python
def probe_frame_count(path: Path) -> int:
    """Return the number of video frames in *path*, on the 25 fps grid.

    WebM reports ``nb_frames`` as ``N/A``, so the count is derived from the
    container duration. Fails loud when the duration is not a clean multiple of
    the frame interval, which would mean the input is not the CFR material the
    whole time model assumes.
    """
    duration = probe_duration(path)
    exact = duration * FPS
    frames = int(round(exact))
    if abs(exact - frames) > 0.1:
        raise TimelineError(
            f"{path} is {duration}s, which is not a whole number of {FPS}fps frames "
            f"({exact:.3f}) — the recording is not on the expected frame grid"
        )
    return frames


def assert_recording_fps(path: Path) -> None:
    """Fail loud unless *path*'s video stream is exactly ``25/1``.

    We assert rather than adapt: 25 is a hardcoded Playwright constant, so a
    different value means the recorder changed under us. Silently re-quantising
    the audio timeline onto a new grid would turn that into a subtle desync.
    """
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
    )
    rate = proc.stdout.strip()
    if rate != f"{FPS}/1":
        raise TimelineError(
            f"{path} reports {rate!r} fps, expected {FPS}/1 — "
            "the frame-exact time model does not hold for this input"
        )


def apply_time_edits(src: Path, timeline: Timeline, out: Path) -> None:
    """Rewrite *src* into *out* with *timeline*'s freezes and cuts applied."""
    src = Path(src)
    out = Path(out)
    if not src.exists():
        raise FileNotFoundError(src)
    if timeline.is_empty:
        raise TimelineError("apply_time_edits called with an empty timeline")

    actual = probe_frame_count(src)
    if actual != timeline.source_frames:
        raise TimelineError(
            f"timeline was built for {timeline.source_frames} frames "
            f"but {src} has {actual}"
        )

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-filter_complex",
        build_filtergraph(timeline),
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-vsync",
        "cfr",
        "-movflags",
        "+faststart",
    ]
    _run_to_output(cmd, out)
```

Add to the import block at the top of the module:

```python
from pathlib import Path

from guidebot_recorder.video.mux import (
    _run,
    _run_to_output,
    ffmpeg_bin,
    ffprobe_bin,
    probe_duration,
)
```

- [ ] **Step 8: Run to verify they pass**

Run: `uv run pytest tests/unit/video/test_timeline_ffmpeg.py -v`
Expected: all PASS

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff format guidebot_recorder/video/timeline.py tests/unit/video/
uv run ruff check guidebot_recorder/video/timeline.py tests/unit/video/
git add guidebot_recorder/video/timeline.py tests/unit/video/test_timeline.py tests/unit/video/test_timeline_ffmpeg.py
git commit -m "feat(timeline): frame-exact ffmpeg stage for freeze and cut"
```

---

### Task 3: Configuration and CLI flags

**Files:**
- Modify: `guidebot_recorder/models/config.py` (the `Config` class, after `popup:`)
- Modify: `guidebot_recorder/cli.py` (the `render` command)
- Test: `tests/unit/models/test_config.py` (append — the file exists)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Config.hold_frame_for_narration: bool`, `Config.hold_frame_settle: float`; CLI options `--no-hold-frame`, `--hold-frame-settle`, `--dump-timeline`.

- [ ] **Step 1: Write the failing config tests**

Append to `tests/unit/models/test_config.py`:

```python
def test_hold_frame_defaults_to_on() -> None:
    cfg = Config(
        title="t",
        viewport=Viewport(width=1280, height=720),
        tts=TtsConfig(lang="pl", voice="pl-PL-ZofiaNeural"),
    )
    assert cfg.hold_frame_for_narration is True
    assert cfg.hold_frame_settle == 1.0


def test_hold_frame_accepts_camel_case_aliases() -> None:
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"lang": "pl", "voice": "pl-PL-ZofiaNeural"},
            "holdFrameForNarration": False,
            "holdFrameSettle": 0.5,
        }
    )
    assert cfg.hold_frame_for_narration is False
    assert cfg.hold_frame_settle == 0.5


def test_hold_frame_settle_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"lang": "pl", "voice": "pl-PL-ZofiaNeural"},
                "holdFrameSettle": -1.0,
            }
        )


def test_hold_frame_is_not_part_of_config_hash() -> None:
    """Render-only pacing must never invalidate compiled references."""
    base = {
        "title": "t",
        "viewport": {"width": 1280, "height": 720},
        "tts": {"lang": "pl", "voice": "pl-PL-ZofiaNeural"},
    }
    a = Config.model_validate(base)
    b = Config.model_validate({**base, "holdFrameForNarration": False, "holdFrameSettle": 3.0})
    assert config_hash(a) == config_hash(b)
```

Ensure the file imports `pytest`, `ValidationError` from `pydantic`, and `Config`, `TtsConfig`, `Viewport`, `config_hash` from `guidebot_recorder.models.config`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/models/test_config.py -k hold_frame -v`
Expected: FAIL — `ValidationError: Extra inputs are not permitted` / `AttributeError`

- [ ] **Step 3: Add the config fields**

In `guidebot_recorder/models/config.py`, inside `class Config`, directly after the `popup:` field:

```python
    # --- Render pacing (render-only; deliberately absent from config_hash) ---
    # Holding a still frame instead of waiting out the voice-over. The narration
    # still plays in full; it is the picture that stops. `hold` matches the sense
    # it already carries in `step.slide.hold`.
    hold_frame_for_narration: bool = Field(default=True, alias="holdFrameForNarration")
    # Real seconds recorded before the frame is held, paid OUT OF the narration
    # (not on top of it) so the finished film keeps its length. Gives entry
    # animations triggered by this step time to finish before the picture stops.
    hold_frame_settle: float = Field(default=1.0, alias="holdFrameSettle", ge=0)
```

`config_hash` is a whitelist projection (`config.py:268-274`), so no change is needed there — the new fields are excluded automatically. The last test in Step 1 locks that in.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/models/test_config.py -k hold_frame -v`
Expected: all PASS

- [ ] **Step 5: Add the CLI flags**

In `guidebot_recorder/cli.py`, add to the `render` command signature, following the existing option style:

```python
    hold_frame: bool = typer.Option(
        True,
        "--hold-frame/--no-hold-frame",
        help="Zamroź klatkę na czas narracji zamiast czekać w czasie rzeczywistym.",
    ),
    hold_frame_settle: float | None = typer.Option(
        None,
        "--hold-frame-settle",
        help="Sekundy realnego czasu przed zamrożeniem klatki (domyślnie z konfiguracji).",
    ),
    dump_timeline: bool = typer.Option(
        False,
        "--dump-timeline",
        help="Zapisz wyliczoną oś czasu obok pliku wideo (diagnostyka).",
    ),
```

Apply them as overrides over the scenario config before calling `run_render`, matching how the command already threads options through:

```python
    if not hold_frame:
        cfg.hold_frame_for_narration = False
    if hold_frame_settle is not None:
        cfg.hold_frame_settle = hold_frame_settle
```

Pass `dump_timeline=dump_timeline` into `run_render` (the parameter is added in Task 5).

- [ ] **Step 6: Verify the CLI still loads**

Run: `uv run guidebot-recorder render --help`
Expected: help text lists `--hold-frame/--no-hold-frame`, `--hold-frame-settle`, `--dump-timeline`

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format guidebot_recorder/models/config.py guidebot_recorder/cli.py tests/unit/models/test_config.py
uv run ruff check guidebot_recorder/models/config.py guidebot_recorder/cli.py tests/unit/models/test_config.py
git add guidebot_recorder/models/config.py guidebot_recorder/cli.py tests/unit/models/test_config.py
git commit -m "feat(config): hold_frame_for_narration and hold_frame_settle"
```

---

### Task 4: Wire into the render pipeline

The behavioural change. Three edits: stop sleeping, store raw timestamps, insert the stage.

**Files:**
- Modify: `guidebot_recorder/recorder/render.py` (lines 393-397, 893-898, 966-977, 1109-1169)
- Test: `tests/unit/recorder/test_render.py` (append)

**Interfaces:**
- Consumes: `TimeEdit`, `Timeline`, `apply_time_edits`, `assert_recording_fps`, `probe_frame_count`, `seconds_to_frames` from Task 2; `Config.hold_frame_for_narration`, `Config.hold_frame_settle` from Task 3.
- Produces: `_pace_narration(...)` (module-private).

- [ ] **Step 1: Write the failing pacing tests**

Append to `tests/unit/recorder/test_render.py`:

```python
import asyncio
import time

from guidebot_recorder.recorder.render import _pace_narration
from guidebot_recorder.video.timeline import TimeEdit


class _Seg:
    def __init__(self, duration: float) -> None:
        self.duration = duration


async def test_pace_narration_sleeps_in_full_when_disabled() -> None:
    edits: list[TimeEdit] = []
    started = time.monotonic()
    await _pace_narration(
        [_Seg(0.3)], anchor=started, hold_frame=False, settle=0.1, edits=edits
    )
    assert time.monotonic() - started >= 0.3
    assert edits == []


async def test_pace_narration_records_a_freeze_for_the_remainder() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration(
        [_Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits
    )
    elapsed = time.monotonic() - anchor
    # Only the settle is paid in real time.
    assert elapsed < 1.0
    assert len(edits) == 1
    assert edits[0].kind == "freeze"
    # 2.0s narration - 0.1s settle = 1.9s -> 48 frames (rounded to the grid)
    assert edits[0].frames == 48


async def test_pace_narration_uses_the_longest_language() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration(
        [_Seg(0.5), _Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits
    )
    assert edits[0].frames == 48


async def test_pace_narration_emits_no_freeze_when_narration_is_shorter_than_settle() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration(
        [_Seg(0.2)], anchor=anchor, hold_frame=True, settle=1.0, edits=edits
    )
    assert time.monotonic() - anchor >= 0.2
    assert edits == []


async def test_pace_narration_ignores_empty_segments() -> None:
    edits: list[TimeEdit] = []
    await _pace_narration([], anchor=time.monotonic(), hold_frame=True, settle=1.0, edits=edits)
    assert edits == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/recorder/test_render.py -k pace_narration -v`
Expected: FAIL — `ImportError: cannot import name '_pace_narration'`

- [ ] **Step 3: Replace the narration sleep**

In `guidebot_recorder/recorder/render.py`, replace `_wait_for_step_narration` (lines 393-397) with:

```python
async def _pace_narration(
    segments: list[Segment],
    *,
    anchor: float,
    hold_frame: bool,
    settle: float,
    edits: list[TimeEdit],
) -> None:
    """Pace one shared visual step by its longest configured narration.

    With ``hold_frame`` the wall clock only pays ``settle`` seconds — enough for
    entry animations triggered by this step to finish — and the rest of the
    voice-over becomes a held frame inserted in post. The settle comes *out of*
    the narration, not on top of it, so the finished film keeps the exact pacing
    it had when the renderer slept through the whole thing.
    """
    if not segments:
        return
    duration = max(segment.duration for segment in segments)

    if not hold_frame:
        await asyncio.sleep(duration)
        return

    real = min(settle, duration)
    await asyncio.sleep(real)

    remaining = duration - real
    if remaining <= 0:
        return
    edits.append(
        TimeEdit(
            at=seconds_to_frames(time.monotonic() - anchor),
            kind="freeze",
            frames=seconds_to_frames(remaining),
        )
    )
```

Add to the imports at the top of `render.py`:

```python
from guidebot_recorder.video.timeline import (
    TimeEdit,
    Timeline,
    apply_time_edits,
    assert_recording_fps,
    probe_frame_count,
    seconds_to_frames,
)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/recorder/test_render.py -k pace_narration -v`
Expected: all PASS

- [ ] **Step 5: Store raw timestamps and update the call site**

In `run_render`, next to `sfx_events` (line 893), add the edit list:

```python
    time_edits: list[TimeEdit] = []
```

Replace the narration-offset block (lines 966-977) so it stores the **raw** wall-clock offset — the mapping needs the complete edit list, which does not exist yet at this point:

```python
                narration_offset = time.monotonic() - anchor
                placed_by_language[tts.lang].append(
                    Placed(segment=seg, offset=narration_offset)
                )
        if step_segments:
            # One picture timeline: the action waits for the longest language,
            await _pace_narration(
                step_segments,
                anchor=anchor,
                hold_frame=cfg.hold_frame_for_narration,
                settle=cfg.hold_frame_settle,
                edits=time_edits,
            )
```

- [ ] **Step 6: Insert the time-edit stage**

Replace the assembly block (lines 1121-1169). Both branches already converge on `_assemble_audio_tracks`, so the stage slots in once, just before the duration is taken:

```python
    main_webm = Path(await video.path())

    if popup is None:
        source_video = main_webm
        preencoded = False
    else:
        popup_webm = Path(await popup.video.path())
        closed_at = probe_duration(main_webm) if popup_open_at_end else popup.closed_at
        assert closed_at is not None
        composite = work / f"{out_mp4.stem}.composite.mp4"
        compose_popup_video(
            main_webm,
            popup_webm,
            composite,
            popup.opened_at,
            closed_at,
            visual_ready_delay=popup.visual_ready_delay,
            transition=cfg.popup.effective_transition,
            slide_ms=cfg.popup.slide_ms,
            scale=cfg.popup.scale,
            corner_radius=cfg.popup.corner_radius,
            shadow=cfg.popup.shadow,
            backdrop_dim=cfg.popup.backdrop_dim,
            backdrop_blur=cfg.popup.backdrop_blur,
            open_ms=cfg.popup.open_ms,
            close_ms=cfg.popup.close_ms,
            hold_open_at_end=popup_open_at_end,
        )
        source_video = composite
        preencoded = True

    # Time editing runs AFTER popup composition: popups are composed on the
    # recording axis and must stay there. Only what is consumed downstream —
    # narration and SFX — moves onto the virtual axis.
    timeline = Timeline.build(time_edits, source_frames=probe_frame_count(source_video))
    if not timeline.is_empty:
        assert_recording_fps(source_video)
        edited = work / f"{out_mp4.stem}.timeline.mp4"
        apply_time_edits(source_video, timeline, edited)
        source_video = edited
        preencoded = True

    total = timeline.virtual_duration
    placed_by_language = {
        lang: [Placed(segment=p.segment, offset=timeline.to_virtual_seconds(p.offset)) for p in ps]
        for lang, ps in placed_by_language.items()
    }
    sfx_offsets = [(kind, timeline.to_virtual_seconds(off)) for kind, off in sfx_offsets]

    await _assemble_audio_tracks(
        source_video,
        audio_configs,
        placed_by_language,
        total,
        work,
        out_mp4,
        preencoded=preencoded,
        sound=cfg.sound,
        sfx_offsets=sfx_offsets,
    )
```

Note: `total` now comes from the model rather than `probe_duration`, which is what makes the audio and video axes agree by construction.

**No guard needs editing — verify this rather than assuming it.** The spec anticipated rewriting three of them; the chosen layering makes all three correct as they stand:

- `mux.py:884-891` (0.05 s tolerance) compares each audio track against `video_duration`, probed from whatever file it is handed. Since it now receives the *edited* video, both sides are already virtual.
- `render.py:472-478` compares narration offsets against `total`. Both are now virtual.
- `mux.py:316-319` guards popup `opened_at`/`closed_at` against the main recording's duration. Popups are composed *before* editing, so this correctly stays on the recording axis.

If a guard does start firing, that is a real bug in the virtual axis — do not widen the tolerance to silence it.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest tests/unit -v`
Expected: all PASS. If any pre-existing test asserts render wall-clock timing, it must be updated to set `holdFrameForNarration: false` explicitly rather than relaxed.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff format guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
uv run ruff check guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git add guidebot_recorder/recorder/render.py tests/unit/recorder/test_render.py
git commit -m "feat(render): hold a still frame instead of sleeping through narration"
```

---

### Task 5: Timeline dump

Diagnosing accumulating drift without seeing the computed axis is miserable, and drift is the failure mode this feature introduces.

**Files:**
- Modify: `guidebot_recorder/video/timeline.py`, `guidebot_recorder/recorder/render.py`
- Test: `tests/unit/video/test_timeline.py` (append)

**Interfaces:**
- Consumes: `Timeline` from Task 1.
- Produces: `Timeline.to_json() -> str`; `run_render(..., dump_timeline: bool = False)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/video/test_timeline.py`:

```python
import json


def test_to_json_describes_both_axes() -> None:
    tl = Timeline.build([TimeEdit(at=75, kind="freeze", frames=59)], source_frames=148)
    payload = json.loads(tl.to_json())
    assert payload["fps"] == 25
    assert payload["source_frames"] == 148
    assert payload["virtual_frames"] == 207
    assert payload["virtual_duration"] == pytest.approx(8.28)
    assert payload["edits"] == [{"at": 75, "kind": "freeze", "frames": 59}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/video/test_timeline.py -k to_json -v`
Expected: FAIL — `AttributeError: 'Timeline' object has no attribute 'to_json'`

- [ ] **Step 3: Implement**

Add `import json` to `guidebot_recorder/video/timeline.py` and this method to `Timeline`:

```python
    def to_json(self) -> str:
        """Serialise both axes for diagnostics."""
        return json.dumps(
            {
                "fps": FPS,
                "source_frames": self.source_frames,
                "virtual_frames": self.virtual_frames,
                "virtual_duration": self.virtual_duration,
                "edits": [
                    {"at": e.at, "kind": e.kind, "frames": e.frames} for e in self.edits
                ],
            },
            indent=2,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/video/test_timeline.py -k to_json -v`
Expected: PASS

- [ ] **Step 5: Wire the flag**

Add `dump_timeline: bool = False` to `run_render`'s signature, and after the `Timeline.build(...)` line in Task 4's block:

```python
    if dump_timeline:
        out_mp4.with_suffix(".timeline.json").write_text(timeline.to_json(), encoding="utf-8")
```

- [ ] **Step 6: Verify end to end**

Run: `uv run pytest tests/unit -q`
Expected: all PASS

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format guidebot_recorder/video/timeline.py guidebot_recorder/recorder/render.py tests/unit/video/test_timeline.py
uv run ruff check guidebot_recorder/video/timeline.py guidebot_recorder/recorder/render.py tests/unit/video/test_timeline.py
git add guidebot_recorder/video/timeline.py guidebot_recorder/recorder/render.py tests/unit/video/test_timeline.py
git commit -m "feat(timeline): optional --dump-timeline diagnostic output"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/en/scenario-reference.md`, `docs/pl/scenario-reference.md`, `docs/en/cli-reference.md`, `docs/pl/cli-reference.md`

- [ ] **Step 1: Document the config fields**

Add to the config-reference table in `docs/en/scenario-reference.md`, matching the surrounding table's columns:

| Field | Default | Meaning |
|---|---|---|
| `holdFrameForNarration` | `true` | Hold a still frame while the voice-over plays instead of recording in real time. The narration is unchanged; the picture stops. Cuts render time roughly by the total length of the narration. |
| `holdFrameSettle` | `1.0` | Seconds of real time recorded before the frame is held, so animations triggered by the step can finish. Paid out of the narration, so the finished film keeps its length. |

State explicitly that the finished film is the same length either way, but **looks different**: with the default on, the page is static under the voice-over where it previously kept animating.

Mirror the same content in Polish in `docs/pl/scenario-reference.md`, following that file's existing tone and table format.

- [ ] **Step 2: Document the CLI flags**

Add to the `render` section of `docs/en/cli-reference.md` and `docs/pl/cli-reference.md`:

- `--no-hold-frame` — record narration in real time (the pre-existing behaviour); use when a scenario's animations must keep running under the voice-over.
- `--hold-frame-settle FLOAT` — override `holdFrameSettle` for this run.
- `--dump-timeline` — write the computed timeline next to the video as `<name>.timeline.json`; useful when audio and video appear to drift.

- [ ] **Step 3: Verify the docs build**

Run: `uv run mkdocs build --strict`
Expected: builds with no warnings

- [ ] **Step 4: Commit**

```bash
git add docs/en docs/pl
git commit -m "docs: document hold-frame render pacing"
```

---

## Verification Before PR

- [ ] `uv run pytest tests/unit -q` — all pass
- [ ] `uv run pytest tests/integration -q` — all pass
- [ ] `uv run ruff check .` — clean
- [ ] `uv run ruff format --check .` — clean
- [ ] `uv run mkdocs build --strict` — clean
- [ ] A real render of an existing scenario completes, and the output plays with narration in sync from the first step to the last. Compare its duration against the same scenario rendered with `--no-hold-frame`: **the two must match within one frame (0.04 s)**. This is the end-to-end proof that the virtual axis is correct.
