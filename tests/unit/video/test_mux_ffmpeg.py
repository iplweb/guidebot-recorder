"""ffmpeg-backed test for ``video.mux.ffmpeg`` — the atomic-output runner seam.

``_run_to_output`` writes to a hidden sibling and renames on success, so a failed
ffmpeg leaves the previous artifact intact. The failure is injected by patching
``_run`` on the *defining* module (``mux_module.ffmpeg``), which is the seam every
mux consumer reads at call time (see ``test_mux_seams.py``).

No shared conftest by design — the marker block comes from the explicitly imported
``_mux_helpers`` (see that module's docstring for why).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.unit.video._mux_helpers import FFMPEG

mux_module = importlib.import_module("guidebot_recorder.video.mux")

pytestmark = FFMPEG


def test_atomic_output_preserves_previous_artifact_after_ffmpeg_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.mp4"
    out.write_bytes(b"previous-good-artifact")

    def fail_after_partial_write(cmd: list[str]):
        Path(cmd[-1]).write_bytes(b"partial")
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(mux_module.ffmpeg, "_run", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        mux_module.ffmpeg._run_to_output(["ffmpeg", "-y"], out)

    assert out.read_bytes() == b"previous-good-artifact"
    assert list(tmp_path.glob(".out.*.mp4")) == []
