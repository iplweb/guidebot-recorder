"""Pure annotation geometry — no I/O, no browser. Coordinates are screenshot pixels."""

from __future__ import annotations

from guidebot_recorder.guide.model import Annotation

#: fixed click-circle radius (screenshot px, deviceScaleFactor already applied upstream)
CLICK_RADIUS = 22.0

_Point = tuple[float, float]


def annotations_for(
    action: str,
    *,
    prev_cursor: _Point | None,
    center: _Point | None,
    box: dict | None,
) -> list[Annotation]:
    """Build the marks for one target action, omitting any mark that lacks geometry."""

    anns: list[Annotation] = []
    if prev_cursor is not None and center is not None:
        anns.append(
            Annotation(kind="arrow", x1=prev_cursor[0], y1=prev_cursor[1], x2=center[0], y2=center[1])
        )
    if action == "click" and center is not None:
        anns.append(Annotation(kind="click", cx=center[0], cy=center[1], r=CLICK_RADIUS))
    elif action == "hover" and box is not None:
        anns.append(Annotation(kind="hover", x=box["x"], y=box["y"], w=box["width"], h=box["height"]))
    elif action == "type" and box is not None:
        anns.append(Annotation(kind="typed", x=box["x"], y=box["y"], w=box["width"], h=box["height"]))
    return anns
