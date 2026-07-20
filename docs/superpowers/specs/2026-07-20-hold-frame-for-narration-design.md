# Hold frame for narration — time as data

**Date:** 2026-07-20
**Status:** approved, ready to plan

## Problem

Render wall-clock time is dominated by narration the renderer already knows the
length of before the browser even starts.

Phase 0 (`recorder/render.py:744`) pre-synthesises every narration segment and
measures each one with `ffprobe`. The browser context is created afterwards, at
`render.py:760`. Yet during recording the renderer sleeps through those exact
durations in real time:

```python
# recorder/render.py:393-397
async def _wait_for_step_narration(segments: list[Segment]) -> None:
    """Pace one shared visual step by its longest configured narration."""
    if segments:
        await asyncio.sleep(max(segment.duration for segment in segments))
```

Called per step at `render.py:974-977`. Render duration is therefore roughly the
sum of all narration plus animation overhead — a cost that is fully computable
in advance and paid anyway.

In practice the page is static while the voice-over plays. Holding a still frame
there loses nothing.

## Root cause

The renderer has no explicit model of time.

Audio offsets are measured mid-recording from the wall clock:

```python
# recorder/render.py:966-972
narration_offset = time.monotonic() - anchor
placed_by_language[tts.lang].append(Placed(segment=seg, offset=narration_offset))
```

`anchor` is set at `render.py:891`. Nothing about the timeline is serialised;
`placed_by_language`, `sfx_events`, `popup.opened_at/closed_at` and `anchor` all
live only inside a single `run_render` call and die with it. The
`*.compiled.yaml` sidecar (`models/action.py:66-79`) carries no timing fields.

So the timeline exists only as elapsed real time. Any feature that reshapes
duration has nowhere to write to. **Time must become data first.**

## Scope

In scope:

- A general time-edit mechanism (`freeze` and `cut`), fully built and tested.
- Freeze applied to narration waits. Enabled by default.

Explicitly out of scope:

- Detecting dead time ("hangs") to feed `cut`. The mechanism supports `cut` and
  it is tested, but nothing emits `cut` edits yet. Deciding where hangs come
  from — explicit `wait:` steps, `load_state` waits, or a stillness threshold —
  is a separate design.
- Any change to cursor glide, typing animation, `settle_ms`, or slide holds.
  These remain real-time. Narration dominates; the rest stays as is.

## Design

### 1. Time model

New module `guidebot_recorder/video/timeline.py` — the only place in the
codebase aware that two time axes exist.

Time is modelled in **integer frames at 25 fps**, never in seconds. See §4 for
the measurements that force this.

```python
FPS = 25  # hardcoded constant in Playwright's video recorder; asserted, not adapted to


@dataclass(frozen=True)
class TimeEdit:
    at: int                        # frame index on the recording axis
    kind: Literal["freeze", "cut"]
    frames: int                    # always positive, whole frames
```

- `freeze` holds the frame at index `at` for `frames` additional frames
- `cut` removes frames `[at, at + frames)` from the recording

Seconds appear only at the boundaries: wall-clock readings convert in with
`round(t_real * FPS)`, and audio offsets convert out with `frames / FPS`.

Two axes result:

- **`t_real`** — the Playwright recording axis (short: only settle pauses remain)
- **`t_virtual`** — the finished film axis (`t_real` plus accumulated edits)

`Timeline` exposes one *mapping*, and every timestamp in the system goes through
it:

```python
def to_virtual(self, frame: int) -> int          # frame index -> frame index
def to_virtual_seconds(self, t_real: float) -> float   # convenience at the boundary
```

(The class also carries `build`, `virtual_frames`, `virtual_duration`,
`is_empty` and `to_json`. The design claim is that there is one mapping — not
one method.)

Its boundary rule is deliberate and load-bearing: a freeze shifts a timestamp
only when `edit.at < frame`, **strictly**. A stamp landing exactly on its own
freeze point is not shifted, because narration begins where the picture stops.
§5 covers the consequence this rule has for the *following* step, which is where
the design's one real hole turned out to be.

This is the load-bearing decision. `anchor` feeds three consumers, and they
split into two groups:

- **Remapped to `t_virtual`:** narration offsets (`render.py:966`) and
  `sfx_events` (`render.py:893-896`). Both are consumed *after* time editing.
  Remapping only narration would drift every key-click and mouse-click by the
  running sum of all freezes — an error that grows over the film, so the opening
  looks right and the ending does not.
- **Stays on `t_real`:** popup `opened_at`/`closed_at` (`render.py:1315-1330`).
  Popup composition runs *before* time editing (§2) and operates entirely on the
  recording axis. Remapping these would be a bug.

Because offsets must be mapped against the *complete* edit list, raw `t_real`
readings are stored during recording and converted once at the end — not
converted inline as they are taken today.

Boundary policy: a timestamp falling *inside* a cut span clamps to the span's
start. Cuts remove dead time, so nothing is lost — but this is an explicit
decision, not an accident of arithmetic.

### 2. Pipeline placement

Time editing is a distinct stage, after popup composition and before audio mux:

```
recording (t_real)
   -> compose_popup_video    (t_real — existing code, UNCHANGED)
   -> apply_time_edits       (t_real -> t_virtual — NEW)
   -> mux_audio_tracks       (t_virtual — guards rewritten)
```

Popup logic keeps operating on the recording axis it already understands:
`opened_at`/`closed_at`, the encoder start-up compensation (`mux.py:281-282`),
and the `crop`-before-`scale` ordering all stay untouched. Freezing a
already-composed frame means popups freeze correctly for free.

### 3. Filtergraph

One graph handles both edit kinds uniformly. All boundaries are **frame
indices**, never float seconds — `trim=start_frame/end_frame` was verified to
produce byte-identical output to the float form while removing all boundary
ambiguity.

- kept span: `trim=start_frame=a:end_frame=b, setpts=PTS-STARTPTS`
- freeze: the run **ending at** frame `K`, with `tpad=stop_mode=clone:stop=N`
  appended — `tpad` clones that run's last frame, which is `K` itself
- cut: **absence** of a span from the kept list

followed by `concat`. Cutting needs no new code branch — it is a missing entry.

Working shape (freeze of 59 frames at source frame 75):

```
[0:v]fps=25,split=2[s0][s1];
[s0]trim=start_frame=0:end_frame=76,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop=59[a];
[s1]trim=start_frame=76,setpts=PTS-STARTPTS[b];
[a][b]concat=n=2:v=1:a=0[v]
```

with `-r 25 -vsync cfr`. A freeze adds `N` net frames; the following segment
resumes at `K + 1`.

**A freeze is folded into the run it terminates rather than emitted as its own
segment, and that is not cosmetic.** The obvious decomposition — a dedicated
one-frame `trim=start_frame=75:end_frame=76` segment — is broken: `concat`
derives an input's duration from the frames it receives (`setpts` having cleared
the frame-rate link), so a **one-frame input measures as zero length**. `concat`
then advances its offset by zero and stacks the next segment on top, and
`-vsync cfr` resolves the collision by dropping a frame.

This was found in production of this very feature: two freezes exactly two
frames apart left a one-frame kept segment between them and the render failed
with "produced 404 frames but the timeline models 405". Measured: at n=1 the CFR
encoder recovers the final file (content verified correct), so the frame count
alone does **not** detect it — a duplicate PTS at the concat output does.
Regression coverage must assert PTS at the concat stage, not the output frame
count.

For the same reason `build_filtergraph` rejects any non-final segment shorter
than two frames (`TimelineError`). Today only one shape can produce it — a lone
kept frame butting against a cut — which is unreachable because nothing emits
cuts yet. Note the asymmetry: `Timeline.build` accepts such a timeline and the
graph builder rejects it, so the failure lands late. If cuts ever ship, the
check belongs in `Timeline.build`, or the runs either side need a `select`-based
merge.

The seam was verified lossless (ffv1 + `framemd5` over every frame): exactly one
run of identical consecutive frames, of exactly the expected length; every other
source frame present exactly once; nothing dropped or duplicated. The freeze
frame's checksum equals the source frame's checksum — pixel-identical.

### 4. Frame quantisation (measured)

Playwright records **strictly CFR at 25 fps**. Verified two ways: every measured
inter-frame delta is exactly 0.040000 s — including a recording whose main
thread was deliberately stalled with synchronous busy-loops, where Chromium
repeats the last frame rather than dropping the grid — and `fps = 25` is a
hardcoded module constant in Playwright's `videoRecorder`, not exposed through
the API, with explicit frame duplication (`repeatCount`) to fill gaps.

The danger is `tpad` rounding to the **nearest** frame on the 40 ms grid, which
puts up to ±20 ms of error into every freeze. Measured:

| `stop_duration` | frames added | actual | error |
|---|---|---|---|
| 2.36 | 59 | 2.36 | 0 ms |
| 2.37 | 59 | 2.36 | **−10 ms** |
| 2.39 | 60 | 2.40 | **+10 ms** |

The error is **additive across freezes**: three freezes of 2.37 s produced
13.000000 s against a naive 13.03 s — **−30 ms**. The `mux.py:827-834` guard
allows 0.05 s, so four or five freezes trip it. The failure would be
data-dependent and read as flaky rather than as systematic drift.

Therefore: **the time model is integer frames end to end, and ffmpeg receives a
frame count (`tpad=stop=N`), not a duration.** Measured with frame-exact values:
three freezes → 13.000000 s, and cut + freeze combined → 7.280000 s, both at
**exactly 0 ms error**.

`fps=25` at the head of the graph is a verified no-op on current input
(`framemd5` identical, 148 frames in and out, no drops or duplicates). It is
kept purely as a defensive normaliser against a future Playwright or ffmpeg
change; it costs nothing.

FPS handling: read `avg_frame_rate` from the probe, parse it as the rational it
is, and **fail loud if it is not exactly `25/1`** rather than adapting. A changed
value would mean a Playwright upgrade altered the recorder — worth noticing
loudly, not silently re-quantising the entire audio timeline onto a new grid.

Implementation note: WebM reports `nb_frames` as `N/A`; use `-count_frames` or
`round(duration * 25)`.

### 5. Configuration

New section in `models/config.py`:

| Field | Default | Meaning |
|---|---|---|
| `hold_frame_for_narration` | `True` | hold a still frame instead of waiting out narration |
| `hold_frame_settle` | `1.0` | seconds of real time waited before each freeze point; floor of `2/FPS` |

CLI: `--no-hold-frame`, `--hold-frame-settle`, `--dump-timeline`. The
hold-frame flag is tri-state — unset defers to the scenario, so it turns holding
both on and off. Overrides are validated against the same bounds as the config
fields (`validate_assignment` on the model), so a bad `--hold-frame-settle` is
rejected before the browser launches.

The `2/FPS` floor: one frame is the smallest interval the axis can represent,
and a hold beginning before its step has drawn a frame has nothing to hold. That
argument justifies one frame; the second is a deliberate conservative margin,
not a proven requirement. **The floor does not prevent narration collision** —
see the sequence below, where that job belongs to monotonic stamping.

The name's subject is the frame, not the narration — the voice-over plays in
full; the picture is what stops. `hold` matches existing vocabulary
(`step.slide.hold`) in the same sense of "keep this image for X".

The settle window exists because the page is only *usually* static. Freezing the
instant narration starts would catch mid-flight entry animations — an accordion
still opening, content still fading in — and hold that half-finished state for
seconds.

To be unambiguous, for a step whose longest narration runs `D` seconds:

1. The narration offset is recorded **at the start** of the settle window.
2. `min(settle, D)` seconds of real time elapse — the recording keeps rolling,
   so entry animations triggered by this step play normally, under the voice.
3. A freeze of `D - settle` seconds is inserted at the frame reached. If
   `D <= settle` no freeze is emitted at all and the step behaves exactly as it
   does today.

The settle is therefore paid **at each freeze point**, not once per page load —
and, importantly, it is paid *out of* the narration, not on top of it. Total
virtual time for the step stays exactly `D`, so **the finished film has the same
length and pacing as before**; only recording wall-clock shrinks. A step with 6 s
of narration costs 1 s of recording instead of 6 s.

Falling back to real time when `D <= settle` also means short narrations lose
nothing: there is no freeze to get wrong.

**Step 4, which the first draft of this spec omitted — and that omission was the
design's one real defect.** The sequence above says where a step's *own* stamps
go. It says nothing about the *next* step's. A freeze is stamped at frame `F`;
the following step's narration is stamped a few CDP round-trips later. When that
work takes less than one frame, both land on `F` — and `to_virtual`'s strict
`edit.at < frame` rule (§1) then maps the new narration to the **start** of the
hold, so it plays on top of the previous voice-over.

This was not theoretical. Measured on a real render, eight consecutive 2.0 s
`say` steps **at the default 1.0 s settle**: two of seven gaps came out at 1.0 s
instead of 2.0 s — a one-second overlap followed by a second of dead air. Sound
effects share the root cause.

It survived four reviews because **every guard in this design compares lengths,
never placements**: the film's total length stayed exactly correct, so the
frame-count check, the mux tolerance and the narration-overrun guard all passed.

Therefore: every wall-clock reading that becomes a placement goes through a
single clamped stamping function, `max(seconds_to_frames(now - anchor),
last_freeze_frame + 1)`. The `+1` is exact rather than generous — it is precisely
what cancels the double rounding of splitting `D` into settle and remainder.
Placements are carried as integer frames end to end; seconds reappear only when
the audio bed is built.

The invariant that must be tested — and that nothing tested until it broke — is
that **no narration starts before its predecessor ends**.

Consequence worth stating plainly: **defaulting to `True` changes the appearance
of already-produced films.** Re-rendering an existing scenario yields a still
frame where the page previously animated under the voice-over. This is intended,
but it is not a purely performance-neutral change.

### 6. Guards

This spec originally predicted that three guards assume the audio and video axes
are identical and would all need rewriting onto `t_virtual`. **None of them were
touched.** The layering in §2 makes them correct as they stand, and that is worth
recording as a property of the design rather than an accident:

- `mux.py` audio/video duration comparison (0.05 s tolerance) — receives the
  *edited* video, so both sides are already virtual
- `render.py` "narration exceeds video length" — compares offsets against
  `total`, both now virtual
- `mux.py` popup `opened_at`/`closed_at` bounds — correctly stays on the
  recording axis, because composition precedes editing

One caveat found in review: the mux tolerance is **not** the safety net it looks
like. `_assemble_audio_tracks` passes `video_duration=total` rather than probing,
so that guard compares the model against itself. Before this change `total` came
from `probe_duration`, which anchored it to reality; the model-based `total`
removed that anchor silently. Hence the explicit check below.

Guards this design *does* add:

- **`Timeline.build`** — edits sorted, non-overlapping, positive, within bounds.
  Runs before ffmpeg; fails immediately rather than surfacing as a tolerance
  mismatch half an hour later.
- **`_apply_timeline_edits`** — after the ffmpeg stage, asserts
  `probe_frame_count(edited) == timeline.virtual_frames`, **exact integer
  equality**, no tolerance. This is the only thing verifying the model against
  reality, and it earned its place immediately: it caught the `concat`
  one-frame-segment defect described in §3.
- **`_build_timeline`** — sanitises collected edits before the model sees them:
  clamps an `at` at or beyond `source_frames` to the last frame, and merges
  same-frame freezes by *summing* their holds. Merging is the semantically right
  answer, not a workaround — if two steps want the picture held at one frame, the
  film should hold it for the total. With monotonic stamping (§5) the merge path
  is now reachable only via the end-of-recording clamp; it is kept general.

### 7. Timeline dump

Optional JSON dump of the computed timeline alongside the video
(`--dump-timeline`). Diagnosing accumulating drift without visibility into the
computed axis is painful — and drift is precisely the class of bug this change
introduces. Cost is a few dozen lines.

## Testing

The core — `Timeline.to_virtual` — is a pure function with no ffmpeg, browser or
I/O. Test weight goes there:

- freezes only, cuts only, the two interleaved
- timestamps exactly on an edit boundary
- a timestamp inside a cut span (clamp policy)
- quantisation: seconds converted in at the boundary round to the expected frame
  counts, and the sum of edits equals the final length exactly, in frames
- a scenario with five freezes: total drift is exactly zero (the regression that
  would otherwise trip the `mux.py` 0.05 s guard)

**Length tests are not enough — this is the spec's hardest-won lesson.** Every
defect found in review produced a film of exactly the right length and was
invisible to length assertions. Two invariants must be asserted directly:

- **Placement:** no narration starts before its predecessor ends, and no sound
  effect lands inside a hold. Measured on real renders with consecutive fast
  steps, where the collision actually occurs.
- **PTS at the concat stage**, not the final frame count: `-vsync cfr` repairs a
  single collision in the output file, so the frame count stays right while the
  intermediate stream is wrong.

Both must be falsified — shown to fail against the unfixed code — or they are
decoration. One caveat carried forward: the SFX placement test is inherently
probabilistic (the clamp only engages when consecutive events fall inside one
frame), so it detects a regression roughly one run in five rather than reliably.
- filtergraph generation compared against a golden string

Beyond that:

- one integration test on a short scenario: final duration and narration offsets
  land where predicted
- regression: the existing suite passes with `hold_frame_for_narration=False`

## Decisions

1. Time becomes data — `video/timeline.py`, one `to_virtual` for all timestamps
2. Freeze by cloning a recorded frame (`tpad=clone` on the run it terminates), pixel-identical seam
3. Time editing is its own stage, between popup composition and audio mux
4. Integer frames at 25 fps end to end; ffmpeg gets `tpad=stop=N`, never seconds
5. `cut` built and tested; hang detection deferred to a separate spec
6. Enabled by default, 1 s settle (floor `2/FPS`), opt-out via `--no-hold-frame`
7. Every placement is stamped monotonically past the last freeze — length
   correctness does not imply placement correctness, and only the latter is
   audible

## Rejected alternatives

**Screenshot / PNG as a still.** Extract the frame to PNG and re-insert it with
`-loop 1 -t D`. Measured and rejected: produced 206 frames where 207 were
expected (an off-by-one, −40 ms), and the round-trip through RGB left the PNG
**not** pixel-identical to its source frame (differing `framemd5`), so the held
frame would visibly differ in colour from its neighbours. Also costs an extra
input, temp file and process. Strictly worse than `tpad`.

**`loop` filter instead of `tpad`.** Measured as exactly equivalent (8.280000 s
/ 207 frames), but needs a manual `setpts` rebuild and has easier-to-misuse
`loop`/`size`/`start` semantics. No advantage.

**CDP `Emulation.setVirtualTimePolicy`.** Speeds up the page clock.
`record_video` records wall-clock regardless of the page's virtual time, so this
cannot work in this architecture.
