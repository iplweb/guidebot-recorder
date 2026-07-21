"""Data model for the step-by-step PDF guide (in-memory only, never serialized)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from guidebot_recorder.models.scenario import Step


@dataclass
class Annotation:
    """One overlay mark, in screenshot pixels. Only the fields for `kind` are set."""

    kind: Literal["arrow", "click", "frame", "highlight"]
    # arrow: prev target edge -> current target edge
    x1: float | None = None
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    # click: eight-armed star around the cursor; each arm spans r_inner..r_outer
    cx: float | None = None
    cy: float | None = None
    r_inner: float | None = None
    r_outer: float | None = None
    # frame: rectangle around the box of any targeted action (click / type / hover / select)
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    # highlight: ellipse around the target box (radii, plus its own colour —
    # the marker colour is per step, so it cannot live in the stylesheet)
    rx: float | None = None
    ry: float | None = None
    color: str | None = None


@dataclass
class GuidePage:
    """One PDF page: a screenshot (or none) plus its description and annotations."""

    kind: Literal["step", "navigate", "slide", "text"]
    screenshot: Path | None
    text: str
    heading: str | None
    annotations: list[Annotation] = field(default_factory=list)
    screenshot_size: tuple[int, int] | None = None


def page_text(step: Step) -> str:
    """Right-hand description: caption overrides narration; empty if neither."""

    return step.caption or step.narration() or ""
