"""The skeleton both composite modes wrap around the middle each builds itself.

:mod:`guidebot_recorder.video.mux.floating` and
:mod:`guidebot_recorder.video.mux.slide` differ completely in their middle segment
— one frames the popup over a dimmed page, the other pushes two full-frame layers
across a colour base — and agree on everything around it: the main input is
CFR-normalised (``fps``) *before* a 3-way split, so the always-consumed middle
fills its whole span even when the backgrounded main page emitted no frames there;
``pre`` and ``tail`` are verbatim main; the concat is ``pre? + mid + tail?`` with
``mid`` always in; and the finished file is measured against the main recording
before anyone downstream can inherit a short film.

**Only the skeleton lives here.** The two mid builders stay in their own modules on
purpose: their rise/hold/fall expressions *look* like one shape and are not. Slide's
fall term carries a ``max(0, ...)`` that float's lacks, because a negative push
offset would slide both layers the wrong way past the end of the interval, where a
negative dim would merely be clamped. Unifying them behind a flag renders without
error and is wrong only in a pixel sample — do not.
"""

from __future__ import annotations

from . import ffmpeg, probe
from .ffmpeg import ffmpeg_bin
from .plan import PopupPlan


def cfr_split(plan: PopupPlan) -> str:
    """CFR-normalise the main input and fan it out to the segments in use.

    The ``fps`` filter comes *before* the split so every consumer sees the same
    constant-rate stream: Playwright's screencast is VFR and can emit no frames at
    all while the popup is on top, which would leave the middle segment empty.
    """
    split_targets: list[str] = []
    if plan.has_pre:
        split_targets.append("[main_pre_src]")
    split_targets.append("[main_mid_src]")
    if plan.has_tail:
        split_targets.append("[main_tail_src]")
    main_norm = f"[0:v]fps={plan.rate:.6f},settb=AVTB,setpts=PTS-STARTPTS"
    if len(split_targets) == 1:
        return f"{main_norm}[main_mid_src]"
    return f"{main_norm},split={len(split_targets)}{''.join(split_targets)}"


def concat_or_null(labels: list[str]) -> str:
    """The graph's last chain: a concat, or a ``null`` when there is one segment.

    ``concat=n=1`` is legal but pointless; the ``null`` keeps the single-segment
    graph free of a filter that would have to re-time a stream that is already the
    whole output.
    """
    if len(labels) == 1:
        return f"{labels[0]}null[outv]"
    return f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]"


def composite_graph(plan: PopupPlan, mid: list[str]) -> list[str]:
    """Wrap the caller's *mid* chains in the shared normalise/segment/concat frame.

    *mid* must end by producing ``[mid]``; everything else — including which
    segments exist at all — follows from the plan.
    """
    filters = [cfr_split(plan)]
    labels: list[str] = []

    # --- pre (verbatim main) --------------------------------------------------
    if plan.has_pre:
        filters.append(
            f"[main_pre_src]trim=start=0:end={plan.opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )
        labels.append("[main_pre]")

    filters += mid
    labels.append("[mid]")

    # --- tail (verbatim main) -------------------------------------------------
    if plan.has_tail:
        filters.append(
            f"[main_tail_src]trim=start={plan.closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )
        labels.append("[main_tail]")

    filters.append(concat_or_null(labels))
    return filters


def encode(plan: PopupPlan, filters: list[str]) -> None:
    """Run *filters* over the plan's two inputs and encode the result to H.264."""
    ffmpeg._run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(plan.main),
            "-i",
            str(plan.popup),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ],
        plan.out,
    )


def encode_and_check_length(
    plan: PopupPlan,
    filters: list[str],
    *,
    mode: str,
    empty_source: str,
) -> None:
    """Encode, then fail loud if the CFR *empty_source* still came out empty.

    A composite that lost its middle is short by ~the popup span and would later
    trip the audio-bed duration guards, a long way from the cause. *mode* and
    *empty_source* name the two things that differ between the callers — the mode
    in the message and the filter whose emptiness explains the loss.
    """
    encode(plan, filters)
    produced = probe.probe_duration(plan.out)
    if produced + 0.2 < plan.main_duration:
        raise ValueError(
            f"{mode} composite duration ({produced:.3f}s) is short of main "
            f"({plan.main_duration:.3f}s); the CFR {empty_source} came out empty"
        )
