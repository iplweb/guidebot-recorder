"""The ``slide`` presentation: main pushes out left, the popup pushes in right.

The other composite mode reached from
:func:`~guidebot_recorder.video.mux.compose.compose_popup_video`, which has already
validated the interval and built the shared ``[popup_cut]`` chain this graph
consumes. The skeleton around the middle segment — CFR-normalise, 3-way split,
concat ``pre? + mid + tail?``, the post-encode length guard — is
:mod:`guidebot_recorder.video.mux.composite`, shared with
:mod:`guidebot_recorder.video.mux.floating`. The mid is not: it is two overlays
tiling across a CFR colour base, with none of float's cosmetics and a push
progress that only *looks* like float's dim ramp.
"""

from __future__ import annotations

from . import composite
from .plan import PopupPlan


def _compose_slide(
    plan: PopupPlan,
    *,
    slide_ms: int,
    hold_open_at_end: bool,
) -> None:
    """Assemble and run the sliding-popup composite filtergraph.

    The mid is two overlays over a CFR colour base (VFR-safe timing;
    ``eof_action=repeat`` holds the last real frame if an input is a frame short
    of the base): the main pushes out to the left while the full-size popup pushes
    in from the right, tiling exactly (both driven by the same ``prog``
    expression, so there is never a black seam).
    """

    span = plan.popup_span
    # D_in/D_out clamp to the interval so a short span cannot overrun; the
    # ``<= 0`` guard mirrors float's ``open_ms=0`` guard so ``prog`` never forms
    # a ``t/0`` (which would be inf/NaN and warp the push geometry).
    d_in = min(slide_ms / 1000.0, span / 2.0)
    d_out = min(slide_ms / 1000.0, span - d_in)
    if plan.size is None:
        raise RuntimeError("ffprobe returned non-numeric size")
    width, height = plan.size

    # prog: 0->1 push-in over D_in, hold at 1, then 1->0 push-out over D_out.
    # A collapsed phase (D<=0) becomes the constant "1" (no division). With
    # hold_open_at_end the whole push-out term is dropped so the popup holds.
    # The ``max(0, ...)`` in the fall is what makes this *not* float's backdrop
    # ramp: without it a negative offset would push both layers on past the end
    # of the interval, where a negative dim would merely be clamped.
    rise = "1" if d_in <= 0 else f"min(1,t/{d_in:.6f})"
    if hold_open_at_end or d_out <= 0:
        fall = "1"
    else:
        fall = f"max(0,min(1,({span:.6f}-t)/{d_out:.6f}))"
    prog = rise if fall == "1" else f"min({rise},{fall})"

    mid = [
        # --- mid_main = main[opened:closed] (full-size, NOT scaled) -----------
        f"[main_mid_src]trim=start={plan.opened_at:.6f}:end={plan.closed_at:.6f},"
        "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
        # --- the reused popup cut (verbatim, full-size) -----------------------
        plan.popup_filter,
        # --- CFR colour base pins output timing (VFR-safe) --------------------
        f"color=black:size={width}x{height}:rate={plan.rate:.6f}:duration={span:.6f},"
        "settb=AVTB,setpts=PTS-STARTPTS[base]",
        # --- two overlays: main exits left, popup enters right (same prog) ----
        # ``overlay``'s ``W`` is the base width; the two layers cover [-W*prog,
        # W-W*prog) and [W-W*prog, ...) with the same expression/rounding, so they
        # tile exactly (probe-confirmed: no black seam). ``eof_action=repeat`` (NOT
        # ``pass``, which would show the black base): a fractional ``trim=start=``
        # can leave ``mid_main``/``popup_cut`` one frame short of the CFR base, and
        # ``pass`` would flash black on that final frame (right before the tail);
        # ``repeat`` holds the last real frame while the base pins output length.
        f"[base][mid_main]overlay=x='-W*({prog})':y=0:eof_action=repeat[wmain]",
        f"[wmain][popup_cut]overlay=x='W*(1-({prog}))':y=0:eof_action=repeat,"
        "settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
    ]

    composite.encode_and_check_length(
        plan,
        composite.composite_graph(plan, mid),
        mode="slide",
        empty_source="base",
    )
