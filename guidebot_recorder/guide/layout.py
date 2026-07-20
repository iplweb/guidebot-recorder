"""Compose GuidePage list into one landscape HTML document for Chromium page.pdf()."""

from __future__ import annotations

import html
from pathlib import Path

from guidebot_recorder.guide.model import Annotation, GuidePage

_STYLE = """
@page { size: A4 landscape; margin: 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #1a1a1a; margin: 0; }
.page { display: grid; grid-template-columns: 62% 38%; gap: 6mm; height: 100vh;
        page-break-after: always; align-items: start; }
.page:last-child { page-break-after: auto; }
.shot { position: relative; width: 100%; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }
.shot img { width: 100%; display: block; }
.shot svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.side { padding-top: 2mm; }
.side .heading { font-size: 20px; font-weight: 700; margin: 0 0 4mm; }
.side .body { font-size: 16px; line-height: 1.5; white-space: pre-wrap; }
.slide { grid-column: 1 / -1; display: flex; flex-direction: column; justify-content: center;
         height: 100vh; text-align: center; }
.slide .title { font-size: 40px; font-weight: 800; }
.slide .subtitle { font-size: 24px; color: #555; margin-top: 4mm; }
.arrow { stroke: #e11; stroke-width: 4; fill: none; marker-end: url(#ah); }
.circle { stroke: #e11; stroke-width: 4; fill: none; }
.rect { stroke: #e11; stroke-width: 4; fill: rgba(238,17,17,0.08); }
"""

_ARROW_MARKER = (
    '<defs><marker id="ah" markerWidth="10" markerHeight="10" refX="8" refY="5" '
    'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#e11"/></marker></defs>'
)


def _svg(anns: list[Annotation], size: tuple[int, int]) -> str:
    w, h = size
    parts = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">', _ARROW_MARKER]
    for a in anns:
        if a.kind == "arrow":
            parts.append(
                f'<line class="arrow" x1="{a.x1}" y1="{a.y1}" x2="{a.x2}" y2="{a.y2}"/>'
            )
        elif a.kind == "click":
            parts.append(f'<circle class="circle" cx="{a.cx}" cy="{a.cy}" r="{a.r}"/>')
        elif a.kind in ("typed", "hover"):
            parts.append(
                f'<rect class="rect" x="{a.x}" y="{a.y}" width="{a.w}" height="{a.h}" rx="4"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _shot_page(page: GuidePage) -> str:
    uri = Path(page.screenshot).absolute().as_uri()
    svg = _svg(page.annotations, page.screenshot_size or (1, 1))
    heading = f'<div class="heading">{html.escape(page.heading)}</div>' if page.heading else ""
    body = f'<div class="body">{html.escape(page.text)}</div>' if page.text else ""
    return (
        '<section class="page">'
        f'<div class="shot"><img src="{uri}"/>{svg}</div>'
        f'<div class="side">{heading}{body}</div>'
        "</section>"
    )


def _slide_page(page: GuidePage) -> str:
    title = f'<div class="title">{html.escape(page.heading or page.text)}</div>'
    sub = f'<div class="subtitle">{html.escape(page.text)}</div>' if page.heading and page.text else ""
    return f'<section class="page"><div class="slide">{title}{sub}</div></section>'


def _text_page(page: GuidePage) -> str:
    heading = f'<div class="heading">{html.escape(page.heading)}</div>' if page.heading else ""
    return (
        '<section class="page"><div class="side" style="grid-column:1/-1">'
        f'{heading}<div class="body">{html.escape(page.text)}</div></div></section>'
    )


def render_html(pages: list[GuidePage], *, title: str) -> str:
    body_parts: list[str] = []
    for page in pages:
        if page.kind == "slide":
            body_parts.append(_slide_page(page))
        elif page.screenshot is not None:
            body_parts.append(_shot_page(page))
        else:
            body_parts.append(_text_page(page))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )
