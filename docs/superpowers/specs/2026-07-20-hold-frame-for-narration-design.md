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

`Timeline` exposes one function, and every timestamp in the system goes through
it:

```python
def to_virtual(self, frame: int) -> int          # frame index -> frame index
def to_virtual_seconds(self, t_real: float) -> float   # convenience at the boundary
```

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
- freeze: `trim` of the single frame `[K, K+1)` + `tpad=stop_mode=clone:stop=N`
- cut: **absence** of a span from the kept list

followed by `concat`. Cutting needs no new code branch — it is a missing entry.

Verified working shape (freeze of 59 frames at source frame 75):

```
[0:v]fps=25,split=3[s0][s1][s2];
[s0]trim=start_frame=0:end_frame=75,setpts=PTS-STARTPTS[a];
[s1]trim=start_frame=75:end_frame=76,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop=59[b];
[s2]trim=start_frame=76,setpts=PTS-STARTPTS[c];
[a][b][c]concat=n=3:v=1:a=0[v]
```

with `-r 25 -vsync cfr`. Note the arithmetic: a freeze at frame `K` of `N`
frames emits segment `[K, K+1)` a total of `N+1` times and adds `N` net frames;
the following segment resumes at `K+1`.

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
| `hold_frame_settle` | `1.0` | seconds of real time waited before each freeze point |

CLI: `--no-hold-frame`, `--hold-frame-settle`.

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

Consequence worth stating plainly: **defaulting to `True` changes the appearance
of already-produced films.** Re-rendering an existing scenario yields a still
frame where the page previously animated under the voice-over. This is intended,
but it is not a purely performance-neutral change.

### 6. Guards

Three places assume the audio and video axes are identical; all move to
`t_virtual`:

- `mux.py:827-834` — audio/video duration comparison, 0.05 s tolerance
- `render.py:472-478` — "narration exceeds video length" guard
- `audiobed.py:96` — `-t {total}` timeline length

Plus validation inside `Timeline` itself, run **before** ffmpeg starts: edits
sorted, non-overlapping, cuts within recording bounds. Failure raises
`RenderError` immediately rather than surfacing half an hour later as a
tolerance mismatch.

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
- filtergraph generation compared against a golden string

Beyond that:

- one integration test on a short scenario: final duration and narration offsets
  land where predicted
- regression: the existing suite passes with `hold_frame_for_narration=False`

## Decisions

1. Time becomes data — `video/timeline.py`, one `to_virtual` for all timestamps
2. Freeze by extracting a recorded frame (`trim` + `tpad=clone`), pixel-identical seam
3. Time editing is its own stage, between popup composition and audio mux
4. Integer frames at 25 fps end to end; ffmpeg gets `tpad=stop=N`, never seconds
5. `cut` built and tested; hang detection deferred to a separate spec
6. Enabled by default, 1 s settle, opt-out via `--no-hold-frame`

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
