"""The popup composition entry point: mode dispatch, and the hard cut itself.

:func:`compose_popup_video` is the only way in. It hands the interval to
:mod:`guidebot_recorder.video.mux.plan`, which validates it and builds the
``[popup_cut]`` chain all three presentation modes consume, then picks a mode: it
emits the hard cut here, or hands the plan to
:mod:`guidebot_recorder.video.mux.floating` or
:mod:`guidebot_recorder.video.mux.slide`, which assemble their own filtergraphs.

The two composite modules are separate files rather than one: together they are
the bulk of the package, and each is a self-contained filtergraph whose comments
only make sense next to their own filter chain. The cut stays here because it is
the mode with no cosmetics — it is the concat the plan already implies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from . import composite
from .floating import _compose_floating
from .plan import PopupPlan, plan_popup_cut
from .slide import _compose_slide


def compose_popup_video(
    main: Path,
    popup: Path,
    out: Path,
    opened_at: float,
    closed_at: float,
    *,
    visual_ready_delay: float = 0.0,
    transition: Literal["cut", "float", "slide"] | None = None,
    floating: bool = False,
    scale: float = 0.72,
    corner_radius: int = 14,
    shadow: bool = True,
    backdrop_dim: float = 0.45,
    backdrop_blur: int = 0,
    open_ms: int = 320,
    close_ms: int = 240,
    hold_open_at_end: bool = False,
    slide_ms: int = 400,
    popup_crop: tuple[int, int, int, int] | None = None,
) -> None:
    """Cut between the main-page and popup recordings on one timeline.

    ``opened_at`` and ``closed_at`` are offsets on the main recording's clock.
    ``visual_ready_delay`` is the bounded time from the page event until a frame
    with both visual layers was verified. The resulting picture keeps main on
    screen through that delay, trims any earlier popup frames, then follows the
    popup until ``closed_at`` and returns to main. When ``closed_at`` is the end
    of ``main`` the final segment is omitted.

    Playwright gives every page in the context the same configured frame size,
    so no scaling is applied.  Each segment has its timestamps reset before the
    concat filter and the final stream is encoded once as H.264 for MP4.

    ``transition`` selects the presentation mode explicitly and wins over the
    deprecated ``floating`` alias: ``mode = transition if transition is not None
    else ("float" if floating else "cut")``. ``cut`` is the hard cut above;
    ``float`` is the composite below; ``slide`` pushes the popup in as a
    full-frame window (main translates left and exits while the popup enters from
    the right) over ``slide_ms``, holds full-frame, then pushes right on close.

    When the mode is ``float`` the popup interval is not a hard cut but a
    composite: the main page stays on screen (dimmed by ``backdrop_dim``, with an
    optional ``backdrop_blur``) while the popup is drawn as a centred,
    ``scale``-d, rounded-corner (``corner_radius``) window with a drop
    ``shadow``, fading in over ``open_ms`` and out over ``close_ms``. The
    backdrop is normalised to CFR before the split so a backgrounded main page
    (which may emit zero frames during the interval) still fills the whole
    span. When ``hold_open_at_end`` is true the close fade / un-dim (float) or the
    push-out (slide) is skipped and the popup is held to the last frame. All
    cosmetics have defaults, so existing ``floating=False`` callers are unaffected.

    ``popup_crop`` is ``(width, height, x, y)`` in the popup recording's pixels:
    the popup's *real* window inside the recorded frame. Playwright's
    ``record_video_size`` is context-level, so a popup records onto a canvas the
    size of the main viewport with filler around its actual window; without a
    crop the ``float`` mode would frame that whole canvas. It applies to
    ``float`` only (``cut``/``slide`` show the popup full-frame by design) and is
    optional — omit it and the filtergraph is byte-identical to before.
    """
    plan = plan_popup_cut(
        main,
        popup,
        out,
        opened_at,
        closed_at,
        visual_ready_delay=visual_ready_delay,
        popup_crop=popup_crop,
    )
    mode = transition if transition is not None else ("float" if floating else "cut")

    if mode == "float":
        _compose_floating(
            plan,
            scale=scale,
            corner_radius=corner_radius,
            shadow=shadow,
            backdrop_dim=backdrop_dim,
            backdrop_blur=backdrop_blur,
            open_ms=open_ms,
            close_ms=close_ms,
            hold_open_at_end=hold_open_at_end,
        )
        return

    if mode == "slide":
        _compose_slide(plan, slide_ms=slide_ms, hold_open_at_end=hold_open_at_end)
        return

    _compose_cut(plan)


def _compose_cut(plan: PopupPlan) -> None:
    """Assemble and run the hard cut: ``main[:opened] + popup + main[closed:]``.

    The mode with no cosmetics at all. Unlike the two composite modes it does not
    normalise the main input to CFR: every segment it emits is verbatim main, so
    there is no colour base or backdrop whose length a variable frame rate could
    leave short.
    """
    filters: list[str] = []
    main_sources: dict[str, str] = {}
    if plan.has_pre and plan.has_tail:
        filters.append("[0:v]settb=AVTB,setpts=PTS-STARTPTS,split=2[main_pre_src][main_tail_src]")
        main_sources = {"pre": "[main_pre_src]", "tail": "[main_tail_src]"}
    elif plan.has_pre:
        main_sources = {"pre": "[0:v]"}
    elif plan.has_tail:
        main_sources = {"tail": "[0:v]"}

    labels: list[str] = []
    if plan.has_pre:
        source = main_sources["pre"]
        normalize = "" if plan.has_tail else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start=0:end={plan.opened_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_pre]"
        )
        labels.append("[main_pre]")

    # The shared [popup_cut] the plan built (hoisted for cut/float/slide).
    filters.append(plan.popup_filter)
    labels.append("[popup_cut]")

    if plan.has_tail:
        source = main_sources["tail"]
        normalize = "" if plan.has_pre else "settb=AVTB,setpts=PTS-STARTPTS,"
        filters.append(
            f"{source}{normalize}trim=start={plan.closed_at:.6f},"
            "setpts=PTS-STARTPTS,setsar=1,format=yuv420p[main_tail]"
        )
        labels.append("[main_tail]")

    filters.append(composite.concat_or_null(labels))
    composite.encode(plan, filters)
