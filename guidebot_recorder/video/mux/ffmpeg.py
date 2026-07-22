"""The ffmpeg/ffprobe binaries and the two subprocess wrappers behind everything.

Leaf module: it imports nothing from its siblings, so every other module in the
package can reach the binaries and the runners without a cycle. :data:`SAMPLE_RATE`
lives here for the same reason — modules in the package need it, and a constant
defined in the package's ``__init__`` could not be imported by them.

:func:`_run` and :func:`_run_to_output` are two of the package's three test
**seams** (the third is
:func:`~guidebot_recorder.video.mux.probe.probe_duration`). Tests replace them on
*this module object*, so consumers inside the package must reach them through the
module — ``from . import ffmpeg`` then ``ffmpeg._run(...)`` — never by name. A
name-import binds the value at import time and no patch would reach it;
``tests/unit/video/test_mux_seams.py`` enforces the rule with an AST check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

#: Audio sample rate used everywhere in the montage pipeline (design §8).
SAMPLE_RATE = 48000


def _resolve(binary: str) -> str:
    """Return the absolute path to *binary* or raise a loud error."""
    found = shutil.which(binary)
    if found is None:
        raise RuntimeError(
            f"'{binary}' not found on PATH. Install ffmpeg "
            "(e.g. `brew install ffmpeg`) to render/mux video."
        )
    return found


def ffmpeg_bin() -> str:
    """Path to the ffmpeg executable (fail-loud if absent)."""
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    """Path to the ffprobe executable (fail-loud if absent)."""
    return _resolve("ffprobe")


def _run(
    cmd: list[str],
    *,
    binary: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run *cmd*, capturing output; raise ``RuntimeError`` on failure.

    ``binary`` keeps stdout as ``bytes`` (raw pixel output); stderr is decoded
    either way so failures stay readable. ``timeout`` lets a caller that can live
    without a result cap its wait: ``subprocess.TimeoutExpired`` propagates (the
    child having been killed) and is that caller's to handle.
    """
    proc = subprocess.run(cmd, capture_output=True, text=not binary, check=False, timeout=timeout)
    if proc.returncode != 0:
        stderr = (
            proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        )
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr}")
    return proc


def _run_to_output(cmd: list[str], out: Path) -> None:
    """Run an ffmpeg command atomically, appending a temporary output path.

    The temporary file lives beside the final artifact so ``os.replace`` is atomic.
    A failed command never truncates a previously successful MP4/WAV.
    """

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{out.stem}.",
        suffix=out.suffix,
        dir=out.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _run([*cmd, str(temporary)])
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
