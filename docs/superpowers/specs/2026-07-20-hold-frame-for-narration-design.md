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

```python
@dataclass(frozen=True)
class TimeEdit:
    at: float                      # position on the recording axis (t_real)
    kind: Literal["freeze", "cut"]
    duration: float                # always positive
```

- `freeze` inserts `duration` seconds of held frame at `at`
- `cut` removes the span `[at, at + duration]` from the recording

Two axes result:

- **`t_real`** — the Playwright recording axis (short: only settle pauses remain)
- **`t_virtual`** — the finished film axis (`t_real` plus accumulated edits)

`Timeline` exposes one function, and every timestamp in the system goes through
it:

```python
def to_virtual(self, t_real: float) -> float
```

This is the load-bearing decision. `anchor` currently feeds **three** consumers:
narration offsets (`render.py:966`), `sfx_events` (`render.py:893-896`) and
popup `opened_at`/`closed_at` (`render.py:1315-1330`). Remapping only narration
would drift key-click SFX and popup composition by the running sum of all
freezes — an error that grows over the film, so the opening looks correct and
the ending does not. A single mapping function makes forgetting a consumer
impossible, because there is no other path.

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

One graph handles both edit kinds uniformly:

- kept span: `trim=start=a:end=b, setpts=PTS-STARTPTS`
- freeze: `trim` of a single frame + `tpad=stop_mode=clone:stop_duration=d`
- cut: **absence** of a span from the kept list

followed by `concat`. Cutting needs no new code branch — it is a missing entry.

### 4. Frame quantisation

`tpad=stop_duration=2.37` clones a whole number of frames, so the real result is
2.36 or 2.40. Computing audio offsets from the unrounded value injects tens of
milliseconds per freeze. Across 30 steps that is roughly 0.3 s of accumulating
drift, and the `mux.py:827-834` guard (0.05 s tolerance) only fires at the end,
after a full recording has been paid for.

Therefore: **every `duration` is quantised to a whole number of frames before
any audio offset is computed.** The audio axis then matches the video axis by
construction rather than approximately.

Open implementation question: Playwright writes variable-frame-rate WebM, so
"fps" is not a single number. The likely fix is forcing CFR with an `fps=N`
filter ahead of `trim`. This must be verified during implementation, not
assumed.

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

To be unambiguous: the settle is paid **at each freeze point**, not once per
page load. The renderer waits `hold_frame_settle` seconds of real time, then
freezes the frame reached at that moment. So a step whose narration runs 6 s
costs 1 s of recording instead of 6 s, and entry animations triggered by that
step's action have a second to finish first.

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
- quantisation: the sum of quantised freezes equals the final length to the frame
- filtergraph generation compared against a golden string

Beyond that:

- one integration test on a short scenario: final duration and narration offsets
  land where predicted
- regression: the existing suite passes with `hold_frame_for_narration=False`

## Decisions

1. Time becomes data — `video/timeline.py`, one `to_virtual` for all timestamps
2. Freeze by extracting a recorded frame (`trim` + `tpad=clone`), pixel-identical seam
3. Time editing is its own stage, between popup composition and audio mux
4. Quantise to frames before computing audio
5. `cut` built and tested; hang detection deferred to a separate spec
6. Enabled by default, 1 s settle, opt-out via `--no-hold-frame`

## Rejected alternatives

**Screenshot as a still.** `page.screenshot()` at freeze time, inserted as a
static clip. Conceptually simpler, but the PNG takes a different encoding path
than WebM frames — a real risk of a visible flicker at the seam (subpixel,
colour, scaling). Not acceptable for a production mode.

**CDP `Emulation.setVirtualTimePolicy`.** Speeds up the page clock.
`record_video` records wall-clock regardless of the page's virtual time, so this
cannot work in this architecture.
