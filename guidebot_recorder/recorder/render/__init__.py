"""The `render` phase — deterministic replay + film assembly (§8/§9).

Phase 0: pre-synthesize every configured narration track into the cache.
Render: 0×LLM, fresh browser, single pass; narration drives the pace.
Assembly: Playwright video + language audio beds (ffmpeg), approximate sync (K2).

Resolved actions are read from the separate ``*.compiled.yaml`` sidecar.

**This module is a facade and holds no logic.** It re-exports the names other
packages and the tests use, so ``from guidebot_recorder.recorder.render import ...``
keeps working after the split into submodules:

    errors.py         RenderError, _OptionalAbsent — imported by everything, so
                      extracted first and left with zero internal imports
    constants.py      the two timing budgets with more than one reader
    tasks.py          disposing of asyncio probes the render no longer needs
    popup_detect.py   what the opener asked ``window.open`` for, and whether it
                      called it at all
    popup_crop.py     the three-level crop chain and its CSS→recording conversion
    popup_session.py  the one-main-window-plus-one-popup contract and its records
    pages.py          which page is live and what should be painted on it
    visuals.py        mounting, priming and handing over the injected layers
    narration.py      the narration clock: synthesis, pacing, frame stamping
    timeline.py       observed freezes → validated model → the edited file
    audio.py          audio beds, muxing, and the two-phase artifact publish
    reuse.py          the compiled-sidecar contract as render reads it
    plan.py           everything a render decides before a browser exists
    stage.py          what is on screen now, and the one order the init scripts
                      may be registered in
    _step.py          _render_step (opaque; phase 3 decomposes it)
    _run.py           run_render — the order the phases run in

It also, on purpose, **withholds every name the tests patch**. Nineteen names plus
``os.replace`` are this package's test seams, and five of them
(``_apply_timeline_edits``, ``_assemble_audio_tracks``, ``_pace_narration``,
``_publish_render_artifacts``, ``_render_step``) used to be imported *from here*.
Re-exporting any of them would let ``monkeypatch.setattr(render_module,
"_pace_narration", ...)`` succeed while reaching nobody — the consumer lives in a
submodule and resolves the name from *its own* globals — and a silently dead patch
leaves a test green while it checks nothing. Withholding turns the same mistake
into an immediate ``AttributeError``/``ImportError``, at collection time. Do **not**
"fix" that by adding the missing re-export; patch (and import from) the submodule
whose globals the consumer reads::

    monkeypatch.setattr(render_module.narration, "_pace_narration", fake)
    monkeypatch.setattr(render_module.popup_crop, "detect_content_crop", fake)
    monkeypatch.setattr("guidebot_recorder.recorder.render.audio.os.replace", fake)

Which submodule that is depends on where the name is *defined*, and the two cases
are opposites:

* defined **inside** the package (``_render_step``, ``_pace_narration``,
  ``_AUDIO_BED_CONCURRENCY``, …) — consumers call it through the module object, so
  the patch goes on the **defining** submodule and nobody may name-import it;
* defined **outside** it (``Recorder``, ``compose_popup_video``,
  ``probe_frame_count``, …) — the consumer keeps ``from X import name``, so the
  patch goes on the **consuming** submodule, and the name-import *is* the seam.

Ten of the nineteen are the second kind, which is why this package's guard cannot
be the one ``video.mux`` uses. Two names have consumers in two submodules at once —
``Recorder`` (``visuals`` and ``_run``) and ``probe_frame_count`` (``timeline`` and
``_run``) — and each needs **two** patch lines; one line silently stops covering
the other path. ``tests/unit/recorder/test_render_seams.py`` enforces all of it.

The submodules are re-exported as *modules* for exactly that reason, using the
redundant-alias form. The private helpers below (``_PopupSession``,
``_build_timeline``, ``_parse_content_box``, …) look like they break the
withholding rule — tests reach them through this facade. They do not: none of them
is a seam, because nothing patches them. Nor can one quietly become one, which is
what makes leaving them re-exported safe — the moment a test writes
``monkeypatch.setattr(render_module, "_build_timeline", ...)`` the seam scan picks
the name up and ``test_facade_withholds_every_patched_name`` fails on this very
re-export, before the dead patch can leave anything green.
"""

from __future__ import annotations

from . import _run as _run
from . import _step as _step
from . import audio as audio
from . import constants as constants
from . import errors as errors
from . import narration as narration
from . import pages as pages
from . import plan as plan
from . import popup_crop as popup_crop
from . import popup_detect as popup_detect
from . import popup_session as popup_session
from . import reuse as reuse
from . import stage as stage
from . import tasks as tasks
from . import timeline as timeline
from . import visuals as visuals
from ._run import run_render
from .audio import (
    _mux_tracks_for_timeline as _mux_tracks_for_timeline,
)
from .errors import RenderError
from .narration import (
    _presynthesize_narration as _presynthesize_narration,
)
from .pages import (
    _expect_chrome as _expect_chrome,
)
from .pages import navigate_pill_mode
from .popup_crop import POPUP_BBOX_DEGENERATE_RATIO
from .popup_crop import (
    _parse_content_box as _parse_content_box,
)
from .popup_crop import (
    _popup_content_box as _popup_content_box,
)
from .popup_crop import (
    _popup_fills_canvas as _popup_fills_canvas,
)
from .popup_crop import (
    _resolve_popup_crop as _resolve_popup_crop,
)
from .popup_crop import (
    _settle_popup_content_box as _settle_popup_content_box,
)
from .popup_crop import (
    _start_popup_content_box as _start_popup_content_box,
)
from .popup_detect import (
    _POPUP_REQUEST_SCRIPT as _POPUP_REQUEST_SCRIPT,
)
from .popup_detect import (
    _parse_window_request as _parse_window_request,
)
from .popup_detect import (
    _popup_window_opened as _popup_window_opened,
)
from .popup_detect import (
    _popup_window_request as _popup_window_request,
)
from .popup_session import (
    _PopupSession as _PopupSession,
)
from .reuse import (
    _compiled_action_is_current as _compiled_action_is_current,
)
from .reuse import (
    _compiled_from as _compiled_from,
)
from .timeline import (
    _build_timeline as _build_timeline,
)
from .visuals import (
    _ensure_visuals as _ensure_visuals,
)
from .visuals import (
    _hand_cursor_to_popup as _hand_cursor_to_popup,
)
from .visuals import (
    _prime_visuals as _prime_visuals,
)

__all__ = [
    "POPUP_BBOX_DEGENERATE_RATIO",
    "RenderError",
    "navigate_pill_mode",
    "run_render",
]
