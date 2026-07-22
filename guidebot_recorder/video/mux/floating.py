"""The ``float`` presentation: the popup as a framed window over a dimmed page.

One of the two composite modes reached from
:func:`~guidebot_recorder.video.mux.compose.compose_popup_video`, which has already
validated the interval and built the shared ``[popup_cut]`` chain this graph
consumes. Its sibling :mod:`guidebot_recorder.video.mux.slide` shares the skeleton
(CFR-normalise, 3-way split, concat ``pre? + mid + tail?``) but nothing else: the
cosmetics here — crop, scale, rounded-corner alpha mask, drop shadow, fades — are
this mode's alone, which is why the two graphs stay in separate modules.
"""

from __future__ import annotations

from pathlib import Path

from . import ffmpeg, probe
from .ffmpeg import ffmpeg_bin


def _compose_floating(
    *,
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    main_duration: float,
    popup_span: float,
    popup_filter: str,
    has_pre: bool,
    has_tail: bool,
    scale: float,
    corner_radius: int,
    shadow: bool,
    backdrop_dim: float,
    backdrop_blur: int,
    open_ms: int,
    close_ms: int,
    hold_open_at_end: bool,
    rate: float,
    popup_crop: tuple[int, int, int, int] | None = None,
) -> None:
    """Assemble and run the floating-popup composite filtergraph.

    Shares the caller's validated trim math (``opened_at``/``closed_at`` already
    shifted by the visual-ready delay, ``popup_filter`` the reused popup cut).
    The main input is CFR-normalised (``fps``) *before* the 3-way split so the
    always-consumed middle segment (``main[opened_at:closed_at]``) fills the
    whole span even when the backgrounded main page emitted no frames there.
    """

    span = popup_span
    open_eff = min(open_ms / 1000.0, span / 2.0)
    close_eff = min(close_ms / 1000.0, span - open_eff)
    filters: list[str] = []

    # --- CFR normalise, then 3-way split (mid is ALWAYS consumed) -------------
    split_targets: list[str] = []
    if has_pre:
        split_targets.append("[main_pre_src]")
    split_targets.append("[main_mid_src]")
    if has_tail:
        split_targets.append("[main_tail_src]")
    main_norm = f"[0:v]fps={rate:.6f},settb=AVTB,setpts=PTS-STARTPTS"
    if len(split_targets) == 1:
        filters.append(f"{main_norm}[main_mid_src]")
    else:
        filters.append(f"{main_norm},split={len(split_targets)}{''.join(split_targets)}")

    # --- pre (verbatim main) --------------------------------------------------
    if has_pre:
        filters.append(
            f"[main_pre_src]trim=start=0:end={opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )

    # --- dimmed backdrop (ramps with the fade so it darkens in step) ----------
    # ``open_ms=0`` (a valid "no open animation" config) makes open_eff 0; guard
    # the division so the eq expression never becomes t/0 (inf/NaN brightness).
    rise = "1" if open_eff <= 0 else f"min(1,t/{open_eff:.6f})"
    if hold_open_at_end or close_eff <= 0:
        ramp = rise
    else:
        fall = f"min(1,({span:.6f}-t)/{close_eff:.6f})"
        ramp = f"min({rise},{fall})"
    dim_expr = f"-{backdrop_dim:.6f}*{ramp}"
    backdrop = (
        f"[main_mid_src]trim=start={opened_at:.6f}:end={closed_at:.6f},"
        f"setpts=PTS-STARTPTS,eq=brightness='{dim_expr}':eval=frame"
    )
    if backdrop_blur > 0:
        backdrop += f",boxblur={backdrop_blur}"
    backdrop += ",setsar=1,format=yuv420p[dim]"
    filters.append(backdrop)

    # --- the reused popup cut -------------------------------------------------
    filters.append(popup_filter)

    # --- framed popup: crop, scale, rounded-corner alpha mask, fade in/out ----
    # The crop must precede the scale so every downstream cosmetic (the alpha
    # mask's W/H, the fade, the blurred shadow) is computed on the popup's real
    # window rather than on the full-viewport canvas it was recorded onto.
    crop_filter = ""
    if popup_crop is not None:
        crop_width, crop_height, crop_x, crop_y = popup_crop
        crop_filter = f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
    r = corner_radius
    # Fully opaque except inside the four corner circles (radius r).
    alpha_expr = (
        f"if(gt(abs(X-(W/2)),(W/2-{r}))*gt(abs(Y-(H/2)),(H/2-{r})),"
        f"if(lte(pow(abs(X-(W/2))-(W/2-{r}),2)+pow(abs(Y-(H/2))-(H/2-{r}),2),pow({r},2)),255,0),"
        "255)"
    )
    framed = (
        f"[popup_cut]{crop_filter}"
        f"scale=trunc(iw*{scale:.6f}/2)*2:trunc(ih*{scale:.6f}/2)*2,"
        "format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'"
    )
    if open_eff > 0:
        framed += f",fade=t=in:alpha=1:d={open_eff:.6f}"
    if not hold_open_at_end and close_eff > 0:
        framed += f",fade=t=out:alpha=1:st={span - close_eff:.6f}:d={close_eff:.6f}"

    # --- overlay onto the dimmed backdrop (backdrop pins the length) ----------
    if shadow:
        framed += ",split=2[framed1][framed2]"
        filters.append(framed)
        # Drop shadow: the popup's (faded) alpha, painted black and blurred, so
        # it fades in step with the window and softly extends past its edges.
        filters.append("[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]")
        filters.append("[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]")
        overlay_base = "[with_shadow][framed1]"
    else:
        framed += "[framed1]"
        filters.append(framed)
        overlay_base = "[dim][framed1]"
    filters.append(
        f"{overlay_base}overlay=x=(W-w)/2:y=(H-h)/2,"
        "settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]"
    )

    # --- tail (verbatim main) -------------------------------------------------
    if has_tail:
        filters.append(
            f"[main_tail_src]trim=start={closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )

    # --- concat pre? + mid + tail? -------------------------------------------
    labels: list[str] = []
    if has_pre:
        labels.append("[main_pre]")
    labels.append("[mid]")
    if has_tail:
        labels.append("[main_tail]")
    if len(labels) == 1:
        filters.append("[mid]null[outv]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

    ffmpeg._run_to_output(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(main),
            "-i",
            str(popup),
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
        out,
    )

    # Fail loud if the CFR backdrop still came out empty: the composite would be
    # short by ~the popup span and later trip the audio-bed duration guards.
    produced = probe.probe_duration(out)
    if produced + 0.2 < main_duration:
        raise ValueError(
            f"floating composite duration ({produced:.3f}s) is short of main "
            f"({main_duration:.3f}s); the CFR backdrop came out empty"
        )
