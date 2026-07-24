"""ffmpeg-backed test for ``video.mux.composite`` — the shared fail-loud guard.

Float and slide share one final ``encode`` step and one guard that fires when the
composite came out shorter than main (its middle segment was lost). The two modes
differ only in the two words naming *which* CFR filter came out empty — the pointer
to the cause — so this asserts the exact message per mode. The empty composite is
provoked by replacing ``composite.encode``, patched on the *defining* module (see
``test_mux_seams.py``); a real empty backdrop would depend on encoder specifics.

No shared conftest by design — the shared builders and the marker block come from
the explicitly imported ``_mux_helpers`` (see its docstring for why).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from guidebot_recorder.video.mux import compose_popup_video
from tests.unit.video._mux_helpers import (
    FFMPEG,
    _make_color_video,
    _make_main_color_timeline,
)

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


@pytest.mark.parametrize(
    ("transition", "message"),
    [
        (
            "float",
            "floating composite duration (1.000s) is short of main (3.000s); "
            "the CFR backdrop came out empty",
        ),
        (
            "slide",
            "slide composite duration (1.000s) is short of main (3.000s); "
            "the CFR base came out empty",
        ),
    ],
)
def test_composite_that_came_out_short_names_its_mode_and_its_empty_filter(
    transition: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-loud guard on a composite that lost its middle segment.

    Both modes share one guard and differ only in the two words that say *which*
    CFR filter came out empty — the pointer to the cause. Asserted on the exact
    string, because a guard that fires with the other mode's wording sends the
    next reader to the wrong filtergraph, and nothing about the failure would
    look wrong. Provoked by replacing the encode: an input that really produces
    an empty backdrop depends on how the encoder handles a sparse VFR source,
    which is not what this is testing.
    """
    main = tmp_path / "main.mp4"
    popup = tmp_path / "popup.mp4"
    out = tmp_path / "composite.mp4"
    _make_main_color_timeline(main)
    _make_color_video(popup, "yellow", 1.0)

    def short_encode(plan, filters: list[str]) -> None:
        _make_color_video(plan.out, "black", 1.0)

    monkeypatch.setattr(mux_module.composite, "encode", short_encode)

    with pytest.raises(ValueError) as excinfo:
        compose_popup_video(main, popup, out, opened_at=1.0, closed_at=2.0, transition=transition)

    assert str(excinfo.value) == message
