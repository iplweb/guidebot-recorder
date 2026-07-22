"""The ``float`` presentation: the popup as a framed window over a dimmed page.

One of the two composite modes reached from
:func:`~guidebot_recorder.video.mux.compose.compose_popup_video`, which has already
validated the interval and built the shared ``[popup_cut]`` chain this graph
consumes. The skeleton around the middle segment — CFR-normalise, 3-way split,
concat ``pre? + mid + tail?``, the post-encode length guard — is
:mod:`guidebot_recorder.video.mux.composite`, shared with
:mod:`guidebot_recorder.video.mux.slide`. Nothing else is: the cosmetics here —
crop, scale, rounded-corner alpha mask, drop shadow, fades, and the backdrop ramp
that darkens in step with them — are this mode's alone.
"""

from __future__ import annotations

from . import composite
from .plan import PopupPlan


def _backdrop(
    plan: PopupPlan,
    *,
    backdrop_dim: float,
    backdrop_blur: int,
    span: float,
    open_eff: float,
    close_eff: float,
    hold_open_at_end: bool,
) -> str:
    """The dimmed main page behind the popup, ramping in step with the fades.

    The ramp rises over the open, holds, and falls over the close, so the page
    darkens exactly as the window appears. It is *not* slide's push progress:
    that one clamps its fall with ``max(0, ...)`` because a negative offset would
    move the layers, where a negative dim is merely clamped by ``eq``. Keep the
    two apart (see :mod:`guidebot_recorder.video.mux.composite`).
    """
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
        f"[main_mid_src]trim=start={plan.opened_at:.6f}:end={plan.closed_at:.6f},"
        f"setpts=PTS-STARTPTS,eq=brightness='{dim_expr}':eval=frame"
    )
    if backdrop_blur > 0:
        backdrop += f",boxblur={backdrop_blur}"
    return backdrop + ",setsar=1,format=yuv420p[dim]"


def _framed_popup(
    popup_crop: tuple[int, int, int, int] | None,
    *,
    scale: float,
    corner_radius: int,
    span: float,
    open_eff: float,
    close_eff: float,
    hold_open_at_end: bool,
) -> str:
    """The popup as a scaled, rounded-corner window that fades in and out.

    The crop must precede the scale so every downstream cosmetic (the alpha
    mask's W/H, the fade, the blurred shadow) is computed on the popup's real
    window rather than on the full-viewport canvas it was recorded onto.
    """
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
    return framed


def _compose_floating(
    plan: PopupPlan,
    *,
    scale: float,
    corner_radius: int,
    shadow: bool,
    backdrop_dim: float,
    backdrop_blur: int,
    open_ms: int,
    close_ms: int,
    hold_open_at_end: bool,
) -> None:
    """Assemble and run the floating-popup composite filtergraph.

    Reads the plan's validated trim math (``opened_at``/``closed_at`` already
    shifted by the visual-ready delay, ``popup_filter`` the reused popup cut) and
    adds only this mode's cosmetics on top.
    """

    span = plan.popup_span
    open_eff = min(open_ms / 1000.0, span / 2.0)
    close_eff = min(close_ms / 1000.0, span - open_eff)

    framed = _framed_popup(
        plan.popup_crop,
        scale=scale,
        corner_radius=corner_radius,
        span=span,
        open_eff=open_eff,
        close_eff=close_eff,
        hold_open_at_end=hold_open_at_end,
    )
    mid = [
        _backdrop(
            plan,
            backdrop_dim=backdrop_dim,
            backdrop_blur=backdrop_blur,
            span=span,
            open_eff=open_eff,
            close_eff=close_eff,
            hold_open_at_end=hold_open_at_end,
        ),
        # --- the reused popup cut ---------------------------------------------
        plan.popup_filter,
    ]

    # --- overlay onto the dimmed backdrop (backdrop pins the length) ----------
    if shadow:
        mid.append(f"{framed},split=2[framed1][framed2]")
        # Drop shadow: the popup's (faded) alpha, painted black and blurred, so
        # it fades in step with the window and softly extends past its edges.
        mid.append("[framed2]geq=r=0:g=0:b=0:a='alpha(X,Y)',boxblur=8[shadow]")
        mid.append("[dim][shadow]overlay=x=(W-w)/2:y=(H-h)/2+6[with_shadow]")
        overlay_base = "[with_shadow][framed1]"
    else:
        mid.append(f"{framed}[framed1]")
        overlay_base = "[dim][framed1]"
    mid.append(
        f"{overlay_base}overlay=x=(W-w)/2:y=(H-h)/2,"
        "settb=AVTB,setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mid]"
    )

    composite.encode_and_check_length(
        plan,
        composite.composite_graph(plan, mid),
        mode="floating",
        empty_source="backdrop",
    )
