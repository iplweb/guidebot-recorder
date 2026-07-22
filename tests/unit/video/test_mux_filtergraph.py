"""Golden filtergraphs: every ``compose_popup_video`` graph, pinned byte for byte.

The rest of the mux suite renders and then samples pixels, which answers "does
ffmpeg accept this" but not "is this the graph we meant". A filtergraph can be
perfectly valid and semantically wrong — two swapped fragments, a dropped guard, a
sign flipped on a push offset — and the film still comes out the right *length*
with the wrong *picture*. Those tests stay green; only a pixel probe aimed at the
exact right region and timestamp would notice.

So this module asserts the whole ``";".join(filters)`` string against a checked-in
literal, for a matrix that reaches every branch of the three graph builders:

    cut    pre+tail / no pre / no tail / neither, startup gap, teardown tail,
           both gaps, visual-ready delay
    float  shadow on+off, blur 0+n, hold_open on+off, open_ms=0, crop set+None,
           clamped span, mid-only
    slide  hold_open on+off, slide_ms=0, clamped span, mid-only

Each case still *runs* ffmpeg (see ``capture_ffmpeg_args``): a string that matches
the literal but that ffmpeg rejects fails here too.

**These literals are the record of today's behaviour, not a specification.** A
change that alters one is either a bug or a deliberate change of picture — never a
formatting detail. Update a literal only together with the reason it changed.

Every number in them is exact because every input is CFR: ``main`` probes as
3.000000s / 25fps / 320x240 and the popups as whole frame counts, so nothing here
depends on ffmpeg's container rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from guidebot_recorder.video.mux import compose_popup_video
from tests.unit.video._mux_helpers import (
    FFMPEG,
    _make_color_video,
    _make_popup_with_filler,
    _make_popup_with_teardown_tail,
    capture_ffmpeg_args,
    filtergraph_of,
)

pytestmark = FFMPEG


@dataclass(frozen=True)
class _Case:
    """One call to :func:`compose_popup_video` and the graph it must emit."""

    popup: str
    """Key into the popup recordings built by :func:`sources`."""
    kwargs: dict[str, Any]
    graph: tuple[str, ...]
    """The chains of ``-filter_complex``, in order; the assertion joins them with ``;``."""


CASES: dict[str, _Case] = {
    # The baseline hard cut: main splits in two, the popup sits between the halves.
    "cut_pre_and_tail": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "cut"},
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][popup_cut][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # opened_at at zero: no [main_pre], and the tail normalises the raw input itself.
    "cut_no_pre": _Case(
        popup="plain",
        kwargs={"opened_at": 0.0, "closed_at": 1.0, "transition": "cut"},
        graph=(
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[popup_cut][main_tail]concat=n=2:v=1:a=0[outv]",
        ),
    ),
    # closed_at at the end of main: no [main_tail], the pre normalises instead.
    "cut_no_tail": _Case(
        popup="plain",
        kwargs={"opened_at": 2.0, "closed_at": 3.0, "transition": "cut"},
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0:end=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_pre][popup_cut]concat=n=2:v=1:a=0[outv]",
        ),
    ),
    # Neither side: no split at all and a `null` in place of the concat.
    "cut_only_popup": _Case(
        popup="plain",
        kwargs={"opened_at": 0.0, "closed_at": 3.0, "transition": "cut"},
        graph=(
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=2.000000,trim=duration=3.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]null[outv]",
        ),
    ),
    # A popup shorter than its interval clones its first frame forward (tpad=start_mode).
    "cut_startup_gap": _Case(
        popup="short",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "cut"},
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=0.600000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=0.400000,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][popup_cut][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # A teardown tail is dropped from the source and paid back by cloning the last good frame.
    "cut_tail_gap": _Case(
        popup="teardown",
        kwargs={
            "opened_at": 1.0,
            "closed_at": 2.0,
            "transition": "cut",
            "popup_crop": (200, 150, 0, 0),
        },
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=0.800000,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.200000,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][popup_cut][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # Both gaps at once — start_mode must precede stop_mode.
    "cut_startup_and_tail_gap": _Case(
        popup="teardown",
        kwargs={
            "opened_at": 1.0,
            "closed_at": 2.2,
            "transition": "cut",
            "popup_crop": (200, 150, 0, 0),
        },
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=0.800000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=0.200000,tpad=stop_mode=clone:stop_duration=0.200000,trim=duration=1.200000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_tail_src]trim=start=2.200000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][popup_cut][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # The prime delay shifts opened_at *and* the popup's trim start.
    "cut_visual_ready_delay": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.5, "transition": "cut", "visual_ready_delay": 0.2},
        graph=(
            "[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.200000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.200000:end=1.000000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=0.500000,trim=duration=1.300000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[main_tail_src]trim=start=2.500000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][popup_cut][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # The full float cosmetic stack: ramped dim, rounded mask, both fades, drop shadow.
    "float_default": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "float"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.320000),min(1,(1.000000-t)/0.240000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,fade=t=out:alpha=1:st=0.760000:d=0.240000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # shadow=False: no [framed2]/[shadow] pair and the overlay reads [dim] directly.
    "float_no_shadow": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "float", "shadow": False},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.320000),min(1,(1.000000-t)/0.240000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,fade=t=out:alpha=1:st=0.760000:d=0.240000[framed1]",
            "[dim][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # backdrop_blur inserts boxblur after the eq, before the setsar.
    "float_blur": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "float", "backdrop_blur": 6},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.320000),min(1,(1.000000-t)/0.240000))':eval=frame,boxblur=6,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,fade=t=out:alpha=1:st=0.760000:d=0.240000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # hold_open_at_end drops the ramp's fall term and the out-fade; the popup is held.
    "float_hold_open": _Case(
        popup="plain",
        kwargs={
            "opened_at": 1.0,
            "closed_at": 2.0,
            "transition": "float",
            "hold_open_at_end": True,
        },
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(1,t/0.320000)':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # open_ms=0 collapses the rise to the constant 1 (never t/0) and drops the in-fade.
    "float_open_ms_zero": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "float", "open_ms": 0},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(1,min(1,(1.000000-t)/0.240000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=out:alpha=1:st=0.760000:d=0.240000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # popup_crop precedes the scale, so every cosmetic is computed on the real window.
    "float_crop": _Case(
        popup="filler",
        kwargs={
            "opened_at": 1.0,
            "closed_at": 2.0,
            "transition": "float",
            "popup_crop": (160, 120, 0, 0),
        },
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.320000),min(1,(1.000000-t)/0.240000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]crop=160:120:0:0,scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,fade=t=out:alpha=1:st=0.760000:d=0.240000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # A short span clamps open_eff/close_eff to half the interval each.
    "float_clamped_span": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 1.4, "transition": "float"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=1.400000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.200000),min(1,(0.400000-t)/0.200000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=0.400000,setpts=PTS-STARTPTS,trim=duration=0.400000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.200000,fade=t=out:alpha=1:st=0.200000:d=0.200000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=1.400000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # No pre and no tail: one split target, and `null` in place of the concat.
    "float_only_mid": _Case(
        popup="plain",
        kwargs={"opened_at": 0.0, "closed_at": 3.0, "transition": "float"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS[main_mid_src]",
            "[main_mid_src]trim=start=0.000000:end=3.000000,setpts=PTS-STARTPTS,eq=brightness='-0.450000*min(min(1,t/0.320000),min(1,(3.000000-t)/0.240000))':eval=frame,setsar=1,format=yuv420p[dim]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=2.000000,trim=duration=3.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "[popup_cut]scale=trunc(iw*0.720000/2)*2:trunc(ih*0.720000/2)*2,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-(W/2)),(W/2-14))*gt(abs(Y-(H/2)),(H/2-14)),if(lte(pow(abs(X-(W/2))-(W/2-14),2)+pow(abs(Y-(H/2))-(H/2-14),2),pow(14,2)),255,0),255)',fade=t=in:alpha=1:d=0.320000,fade=t=out:alpha=1:st=2.760000:d=0.240000,split=2[framed1][framed2]",
            "[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]",
            "[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]",
            "[with_shadow][framed1]overlay=x=(W-w)/2:y=(H-h)/2,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[mid]null[outv]",
        ),
    ),
    # Both layers driven by one prog expression, so they tile without a seam.
    "slide_default": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "slide"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "color=black:size=320x240:rate=25.000000:duration=1.000000,settb=AVTB,setpts=PTS-STARTPTS[base]",
            "[base][mid_main]overlay=x='-W*(min(min(1,t/0.400000),max(0,min(1,(1.000000-t)/0.400000))))':y=0:eof_action=repeat[wmain]",
            "[wmain][popup_cut]overlay=x='W*(1-(min(min(1,t/0.400000),max(0,min(1,(1.000000-t)/0.400000)))))':y=0:eof_action=repeat,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # hold_open_at_end drops the whole push-out term; prog is the bare rise.
    "slide_hold_open": _Case(
        popup="plain",
        kwargs={
            "opened_at": 1.0,
            "closed_at": 2.0,
            "transition": "slide",
            "hold_open_at_end": True,
        },
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "color=black:size=320x240:rate=25.000000:duration=1.000000,settb=AVTB,setpts=PTS-STARTPTS[base]",
            "[base][mid_main]overlay=x='-W*(min(1,t/0.400000))':y=0:eof_action=repeat[wmain]",
            "[wmain][popup_cut]overlay=x='W*(1-(min(1,t/0.400000)))':y=0:eof_action=repeat,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # slide_ms=0 collapses both phases to the constant 1 (no division).
    "slide_ms_zero": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 2.0, "transition": "slide", "slide_ms": 0},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,trim=duration=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "color=black:size=320x240:rate=25.000000:duration=1.000000,settb=AVTB,setpts=PTS-STARTPTS[base]",
            "[base][mid_main]overlay=x='-W*(1)':y=0:eof_action=repeat[wmain]",
            "[wmain][popup_cut]overlay=x='W*(1-(1))':y=0:eof_action=repeat,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=2.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # A short span clamps D_in/D_out to half the interval each.
    "slide_clamped_span": _Case(
        popup="plain",
        kwargs={"opened_at": 1.0, "closed_at": 1.4, "transition": "slide"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS,split=3[main_pre_src][main_mid_src][main_tail_src]",
            "[main_pre_src]trim=start=0:end=1.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]",
            "[main_mid_src]trim=start=1.000000:end=1.400000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=0.400000,setpts=PTS-STARTPTS,trim=duration=0.400000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "color=black:size=320x240:rate=25.000000:duration=0.400000,settb=AVTB,setpts=PTS-STARTPTS[base]",
            "[base][mid_main]overlay=x='-W*(min(min(1,t/0.200000),max(0,min(1,(0.400000-t)/0.200000))))':y=0:eof_action=repeat[wmain]",
            "[wmain][popup_cut]overlay=x='W*(1-(min(min(1,t/0.200000),max(0,min(1,(0.400000-t)/0.200000)))))':y=0:eof_action=repeat,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[main_tail_src]trim=start=1.400000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]",
            "[main_pre][mid][main_tail]concat=n=3:v=1:a=0[outv]",
        ),
    ),
    # No pre and no tail: one split target, and `null` in place of the concat.
    "slide_only_mid": _Case(
        popup="plain",
        kwargs={"opened_at": 0.0, "closed_at": 3.0, "transition": "slide"},
        graph=(
            "[0:v]fps=25.000000,settb=AVTB,setpts=PTS-STARTPTS[main_mid_src]",
            "[main_mid_src]trim=start=0.000000:end=3.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]",
            "[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration=2.000000,trim=duration=3.000000,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[popup_cut]",
            "color=black:size=320x240:rate=25.000000:duration=3.000000,settb=AVTB,setpts=PTS-STARTPTS[base]",
            "[base][mid_main]overlay=x='-W*(min(min(1,t/0.400000),max(0,min(1,(3.000000-t)/0.400000))))':y=0:eof_action=repeat[wmain]",
            "[wmain][popup_cut]overlay=x='W*(1-(min(min(1,t/0.400000),max(0,min(1,(3.000000-t)/0.400000)))))':y=0:eof_action=repeat,settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]",
            "[mid]null[outv]",
        ),
    ),
}


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The recordings the matrix composes from, built once for the whole module.

    ``main`` is 3s at 25fps; each popup is chosen to drive one arm of the trim
    math — ``short`` is a second shy of its interval (startup gap), ``teardown``
    ends with frames whose window no longer fills the crop (tail gap), ``filler``
    is a real window on an oversized canvas (crop).
    """
    directory = tmp_path_factory.mktemp("mux_filtergraph")
    paths = {
        "main": directory / "main.mp4",
        "plain": directory / "popup_plain.mp4",
        "short": directory / "popup_short.mp4",
        "filler": directory / "popup_filler.mp4",
        "teardown": directory / "popup_teardown.mp4",
    }
    _make_color_video(paths["main"], "blue", 3.0)
    _make_color_video(paths["plain"], "yellow", 1.0)
    _make_color_video(paths["short"], "yellow", 0.6)
    _make_popup_with_filler(paths["filler"], 1.0)
    _make_popup_with_teardown_tail(paths["teardown"])
    return paths


@pytest.mark.parametrize("case_id", list(CASES))
def test_compose_popup_video_emits_the_recorded_filtergraph(
    case_id: str,
    sources: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CASES[case_id]
    seen = capture_ffmpeg_args(monkeypatch)

    compose_popup_video(sources["main"], sources[case.popup], tmp_path / "out.mp4", **case.kwargs)

    # One encode per composition: a second run would mean the mode dispatch fell
    # through, which no assertion on the first graph could see.
    assert len(seen) == 1
    assert filtergraph_of(seen[0]) == ";".join(case.graph)
