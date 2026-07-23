"""``run_render``: the whole render pass, top to bottom — and nothing else.

Phase 3 turned the three interleaved lifetimes this function used to carry into
three objects, and what is left here is the order the phases run in:

* :mod:`~guidebot_recorder.recorder.render.plan` —
  :class:`~guidebot_recorder.recorder.render.plan._RenderPlan`, frozen:
  everything decided before a browser exists;
* :mod:`~guidebot_recorder.recorder.render.stage` —
  :class:`~guidebot_recorder.recorder.render.stage._Stage`: what is on screen now,
  and the one order the init scripts may be registered in;
* :mod:`~guidebot_recorder.recorder.render.clock` —
  :class:`~guidebot_recorder.recorder.render.clock._Clock`: the recording axis,
  and the reason ``on_sfx`` is a bound method;
* :mod:`~guidebot_recorder.recorder.render.loop` — replaying the steps, with the
  absence probe split from the narration so the ordering is a seam;
* :mod:`~guidebot_recorder.recorder.render.post` — recording -> composed ->
  virtual -> mastered, in that order and no other.

Both load-bearing orderings therefore live with the code that performs them, not
in this file: the role-gated init scripts in ``stage``, popup composition before
time editing in ``post``. This module holds **no test seam at all** — every one of
them moved to the submodule that constructs or calls it — which is what makes it
short enough to read in one screen.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from playwright.async_api import Browser

from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.tts.base import TtsProvider

from .clock import _Clock
from .loop import _LoopOptions, _run_steps
from .plan import _prepare_render
from .post import _publish_film
from .stage import _open_stage


async def run_render(
    path: Path | str,
    out_mp4: Path | str,
    tts_provider: TtsProvider,
    cache_dir: Path | str,
    browser: Browser,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    pause_on_error: bool = False,
    verbose: bool = False,
    hold_frame: bool | None = None,
    hold_frame_settle: float | None = None,
    dump_timeline: bool = False,
    reasoner: Reasoner | None = None,
) -> None:
    plan = await _prepare_render(
        path,
        out_mp4,
        tts_provider,
        cache_dir,
        env=env,
        hold_frame=hold_frame,
        hold_frame_settle=hold_frame_settle,
        verbose=verbose,
    )
    stage = await _open_stage(browser, plan, env=env, timeout=timeout)
    # Audio placements are collected as recording-axis FRAMES, not seconds — see
    # `clock.py` for why, and for why `note_sfx` is handed over as a bound method.
    clock = _Clock.started(stage.anchor, plan.audio_configs)
    await _run_steps(
        plan,
        stage,
        clock,
        _LoopOptions(
            timeout=timeout,
            pause_on_error=pause_on_error,
            verbose=verbose,
            reasoner=reasoner,
        ),
    )
    await _publish_film(plan, stage, clock, dump_timeline=dump_timeline)
