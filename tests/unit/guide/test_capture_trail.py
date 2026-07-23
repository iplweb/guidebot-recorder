"""Cursor-trail memory and the arrow annotations between steps: where the next
arrow starts, and how `navigate`/`scroll` clear the remembered shape. Driven
with fakes (no real browser).

Per-step-kind replay lives in ``test_capture_replay.py``; select handling in
``test_capture_select.py``; error and pause banners in
``test_capture_errors.py``. Shared fakes come from ``_capture_helpers.py``.
"""

from __future__ import annotations

import pytest

import guidebot_recorder.guide.capture as capture
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.geometry import ray_exit
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import Scenario, Step
from guidebot_recorder.overlay.geometry import ellipse_around, fit_to_bounds

from ._capture_helpers import (
    FakePage,
    FakeRecorder,
    SequenceRecorder,
    _async_none,
    _cfg,
    _compiled,
    _fp,
    _target,
)


async def test_cursor_resets_after_scroll(tmp_path, monkeypatch):
    """A prior action leaves a cursor position; a scroll must clear it.

    Without the reset, the action AFTER the scroll would draw an arrow from
    the stale pre-scroll coordinates to its own (identical, per FakeRecorder)
    center — a sequence that needs an action BEFORE the scroll to be
    observable at all.
    """
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(scroll="down"),
            Step(click="drugi przycisk"),
        ],
    )
    action1 = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    action2 = CachedAction(
        action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
    )
    recorder = FakeRecorder()
    pages = await capture_pages(
        scenario,
        _compiled([action1, None, action2]),
        FakePage(),
        recorder,
        tmp_path / "shots",
        timeout=15.0,
    )
    assert len(pages) == 2
    assert all(a.kind != "arrow" for a in pages[1].annotations)


#: Two targets far enough apart that the clipped arrow between them survives
#: `MIN_ARROW`; the gap runs along x, so the clipped ends are the vertical edges.
_TWO_BOXES = [
    {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
    {"x": 400.0, "y": 0.0, "width": 100.0, "height": 100.0},
]


async def test_arrow_starts_at_the_edge_of_the_previous_target(tmp_path, monkeypatch):
    """The next step's arrow needs the *shape* of the previous target, not just its centre.

    Started in the middle of the previous target the arrow crosses all of it and
    reads as a strikethrough, so capture remembers `prev_shape` alongside
    `prev_cursor` and hands it to `annotations_for`, which clips the start
    against it.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(), steps=[Step(click="pierwszy przycisk"), Step(click="drugi przycisk")]
    )
    actions = [
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        )
        for _ in range(2)
    ]
    recorder = SequenceRecorder(_TWO_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert len(pages) == 2
    arrow = next(a for a in pages[1].annotations if a.kind == "arrow")
    # 50.0 would be the centre of the first target — the pre-clipping behaviour.
    assert arrow.x1 == pytest.approx(100.0)  # right edge of the first box
    assert arrow.x2 == pytest.approx(400.0)  # left edge of the second box


#: Highlight target flush against the right edge, then a click far to the left.
#: `fit_to_bounds` pushes the highlight ellipse inward off the edge, so its fitted
#: rim sits at a different x than the raw one — enough to tell the two apart.
_HIGHLIGHT_EDGE_BOXES = [
    {"x": 1200.0, "y": 340.0, "width": 100.0, "height": 40.0},  # highlight, przy prawej krawędzi
    {"x": 100.0, "y": 340.0, "width": 100.0, "height": 40.0},  # klik, po lewej
]


async def test_arrow_after_a_highlight_starts_on_the_fitted_ellipse(tmp_path, monkeypatch):
    """`prev_shape` po kroku `highlight` musi nieść elipsę DOPASOWANĄ do kadru.

    `capture` zapamiętuje kształt przez `target_shape(..., bounds=size)`. Bez
    `bounds` zapamiętałby elipsę niedopasowaną — a przy celu tuż przy krawędzi
    kadru `fit_to_bounds` przesuwa ją na tyle, że grot następnej strzałki
    startowałby w innym miejscu. Ten test przypina, że start leży na elipsie
    dopasowanej, nie surowej.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[Step(highlight="tabela"), Step(click="drugi przycisk")],
    )
    actions = [
        CachedAction(
            action="highlight",
            target=_target(),
            expect="none",
            fingerprint=_fp(command_kind="highlight"),
        ),
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
    ]
    recorder = SequenceRecorder(_HIGHLIGHT_EDGE_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )

    assert len(pages) == 2
    arrow = next(a for a in pages[1].annotations if a.kind == "arrow")

    padding = _cfg().highlight.padding
    hl_box, click_box = _HIGHLIGHT_EDGE_BOXES
    hl_center = (hl_box["x"] + hl_box["width"] / 2, hl_box["y"] + hl_box["height"] / 2)
    click_center = (
        click_box["x"] + click_box["width"] / 2,
        click_box["y"] + click_box["height"] / 2,
    )

    fitted = fit_to_bounds(ellipse_around(hl_box, padding), width=1280.0, height=720.0)
    unfitted = ellipse_around(hl_box, padding)
    fitted_start = ray_exit(hl_center, click_center, fitted)
    unfitted_start = ray_exit(hl_center, click_center, unfitted)

    # dobór pudełek jest sensowny tylko, jeśli dopasowanie realnie przesuwa start —
    # inaczej test przeszedłby też na elipsie niedopasowanej i niczego by nie chronił
    assert fitted_start[0] != pytest.approx(unfitted_start[0])
    assert (arrow.x1, arrow.y1) == pytest.approx(fitted_start)


def _recording_annotations(calls):
    """Zamiast budować adnotacje, zapisuje `prev_shape` przekazany do każdego kroku akcji."""

    def _f(
        action,
        *,
        prev_cursor,
        prev_shape=None,
        center,
        box,
        row_box=None,
        row_center=None,
        mark=None,
        bounds=None,
    ):
        calls.append(prev_shape)
        return []

    return _f


async def test_navigate_hands_the_next_action_a_cleared_prev_shape(tmp_path, monkeypatch):
    """Zerowanie `prev_shape` po `navigate` musi być OBSERWOWALNE.

    `annotations_for` nie zagląda do `prev_shape`, gdy `prev_cursor is None`, więc
    test na samym braku strzałki przechodzi także po usunięciu `prev_shape = None`.
    Tu podglądamy wprost kwarg dany `annotations_for`: krok po `navigate` musi
    dostać `prev_shape=None`, a nie kształt sprzed przeładowania strony.
    """

    calls: list = []
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    monkeypatch.setattr(capture, "annotations_for", _recording_annotations(calls))
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(navigate="https://example.com/inna"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
        None,
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
    ]
    await capture_pages(
        scenario, _compiled(actions), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )

    assert len(calls) == 2  # dwa kroki akcji; `navigate` nie woła `annotations_for`
    assert calls[0] is None  # pierwszy krok — poprzedniego kształtu jeszcze nie ma
    assert calls[1] is None  # po `navigate` kształt sprzed przeładowania wyzerowany


async def test_scroll_hands_the_next_action_a_cleared_prev_shape(tmp_path, monkeypatch):
    """To samo dla gałęzi `scroll` — dziś nie ma żadnego testu na to zerowanie."""

    calls: list = []
    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    monkeypatch.setattr(capture, "annotations_for", _recording_annotations(calls))
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(scroll="down"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
        None,
        CachedAction(action="click", target=_target(), expect="none", fingerprint=_fp()),
    ]
    await capture_pages(
        scenario, _compiled(actions), FakePage(), FakeRecorder(), tmp_path / "shots", timeout=15.0
    )

    assert len(calls) == 2
    assert calls[1] is None  # po `scroll` kształt sprzed przewinięcia wyzerowany


async def test_shape_memory_resets_after_navigate(tmp_path, monkeypatch):
    """A navigate clears the remembered shape together with the cursor.

    Kept across a page load, `prev_shape` would clip the next arrow against a
    box that belongs to a screenshot the reader never sees. Both are dropped, so
    the step after the navigate opens a fresh arrow-less page.
    """

    monkeypatch.setattr(capture, "reuse_failure", _async_none)
    scenario = Scenario(
        config=_cfg(),
        steps=[
            Step(click="pierwszy przycisk"),
            Step(navigate="https://example.com/inna"),
            Step(click="drugi przycisk"),
        ],
    )
    actions = [
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
        None,
        CachedAction(
            action="click", target=_target(), expect="none", fingerprint=_fp(command_kind="click")
        ),
    ]
    recorder = SequenceRecorder(_TWO_BOXES)
    pages = await capture_pages(
        scenario, _compiled(actions), FakePage(), recorder, tmp_path / "shots", timeout=15.0
    )
    assert [p.kind for p in pages] == ["step", "navigate", "step"]
    assert all(a.kind != "arrow" for a in pages[2].annotations)
