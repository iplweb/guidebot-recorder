"""The ``slide`` presentation: main pushes out left, the popup pushes in right.

The other composite mode reached from
:func:`~guidebot_recorder.video.mux.compose.compose_popup_video`, which has already
validated the interval and built the shared ``[popup_cut]`` chain this graph
consumes. It shares the skeleton of
:mod:`guidebot_recorder.video.mux.floating` (CFR-normalise, 3-way split, concat
``pre? + mid + tail?``) and the post-encode duration guard, but the mid is built
completely differently — two overlays tiling across a CFR colour base — so the two
filtergraphs are kept in separate modules.
"""

from __future__ import annotations

from pathlib import Path

from . import ffmpeg, probe
from .ffmpeg import ffmpeg_bin


def _compose_slide(
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
    slide_ms: int,
    hold_open_at_end: bool,
    rate: float,
    size: tuple[int, int] | None,
) -> None:
    """Assemble and run the sliding-popup composite filtergraph.

    Same skeleton as :func:`~guidebot_recorder.video.mux.floating._compose_floating`:
    the main input is CFR-normalised
    (``fps``) *before* a 3-way split so the always-consumed middle segment
    (``main[opened_at:closed_at]``) fills the whole span even when the
    backgrounded main page emitted no frames there. The mid is two overlays over
    a CFR colour base (VFR-safe timing; ``eof_action=repeat`` holds the last real
    frame if an input is a frame short of the base): the main pushes out to the
    left while the full-size popup pushes in
    from the right, tiling exactly (both driven by the same ``prog`` expression,
    so there is never a black seam). ``pre``/``tail`` are verbatim main. Concat
    ``pre? + mid + tail?`` (mid always in). The post-encode duration fail-loud
    guard transfers unchanged.
    """

    span = popup_span
    # D_in/D_out clamp to the interval so a short span cannot overrun; the
    # ``<= 0`` guard mirrors float's ``open_ms=0`` guard so ``prog`` never forms
    # a ``t/0`` (which would be inf/NaN and warp the push geometry).
    d_in = min(slide_ms / 1000.0, span / 2.0)
    d_out = min(slide_ms / 1000.0, span - d_in)
    if size is None:
        raise RuntimeError("ffprobe returned non-numeric size")
    width, height = size

    # prog: 0->1 push-in over D_in, hold at 1, then 1->0 push-out over D_out.
    # A collapsed phase (D<=0) becomes the constant "1" (no division). With
    # hold_open_at_end the whole push-out term is dropped so the popup holds.
    rise = "1" if d_in <= 0 else f"min(1,t/{d_in:.6f})"
    if hold_open_at_end or d_out <= 0:
        fall = "1"
    else:
        fall = f"max(0,min(1,({span:.6f}-t)/{d_out:.6f}))"
    prog = rise if fall == "1" else f"min({rise},{fall})"

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

    # --- mid_main = main[opened:closed] (full-size, NOT scaled) ---------------
    filters.append(
        f"[main_mid_src]trim=start={opened_at:.6f}:end={closed_at:.6f},"
        "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[mid_main]"
    )

    # --- the reused popup cut (verbatim, full-size) ---------------------------
    filters.append(popup_filter)

    # --- CFR colour base pins output timing (VFR-safe) ------------------------
    filters.append(
        f"color=black:size={width}x{height}:rate={rate:.6f}:duration={span:.6f},"
        "settb=AVTB,setpts=PTS-STARTPTS[base]"
    )

    # --- two overlays: main exits left, popup enters right (same prog) --------
    # ``overlay``'s ``W`` is the base width; the two layers cover [-W*prog,
    # W-W*prog) and [W-W*prog, ...) with the same expression/rounding, so they
    # tile exactly (probe-confirmed: no black seam). ``eof_action=repeat`` (NOT
    # ``pass``, which would show the black base): a fractional ``trim=start=``
    # can leave ``mid_main``/``popup_cut`` one frame short of the CFR base, and
    # ``pass`` would flash black on that final frame (right before the tail);
    # ``repeat`` holds the last real frame while the base pins output length.
    filters.append(f"[base][mid_main]overlay=x='-W*({prog})':y=0:eof_action=repeat[wmain]")
    filters.append(
        f"[wmain][popup_cut]overlay=x='W*(1-({prog}))':y=0:eof_action=repeat,"
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

    # Fail loud if the CFR base still came out empty: the composite would be short
    # by ~the popup span and later trip the audio-bed duration guards.
    produced = probe.probe_duration(out)
    if produced + 0.2 < main_duration:
        raise ValueError(
            f"slide composite duration ({produced:.3f}s) is short of main "
            f"({main_duration:.3f}s); the CFR base came out empty"
        )
