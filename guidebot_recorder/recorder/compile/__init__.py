"""The `compile` phase — algorithm §5.6.

Runs the scenario sequentially on a fresh session; for steps that need a target it
calls the Reasoner (only when there is no valid cache), validates, and freezes the
``cachedAction``. The LLM only returns data; Playwright performs the actions.

The source scenario is read-only: resolved actions are written to a separate
``*.compiled.yaml`` (a list aligned by index to the steps).

The viewport is taken from ``config`` — it MUST match the render phase, otherwise
the frozen element positions do not line up ("element outside of the viewport").

**This module is a facade and holds no logic.** It re-exports the names other
packages and the tests use, so ``from guidebot_recorder.recorder.compile import ...``
keeps working after the split into submodules:

    cache.py     the sidecar reuse contract — what may be reused, what recompiles
    describe.py  human-facing text: step descriptions and warning banners
    pages.py     the one-main-window-plus-one-popup session contract
    step.py      _compile_step: resolve, perform, freeze one step
    run.py       the compile loop and the context that wraps it

It also, on purpose, **withholds two names**: ``write_compiled`` (consumed by the
``checkpoint`` closure in :mod:`~guidebot_recorder.recorder.compile.run`) and
``resolve_step_target`` (consumed by ``_compile_step`` in
:mod:`~guidebot_recorder.recorder.compile.step`). Those two are the package's test
seams. Re-exporting them would let ``monkeypatch.setattr(compile_module,
"write_compiled", ...)`` succeed while reaching nobody — the consumer lives in a
submodule and resolves the name from *its own* globals — and a silently dead patch
leaves a test green while it checks nothing. Withholding turns the same mistake
into an immediate ``AttributeError``. Do **not** "fix" that by adding the missing
re-export; patch the consuming submodule instead::

    monkeypatch.setattr(compile_module.run, "write_compiled", fake)
    monkeypatch.setattr(compile_module.step, "resolve_step_target", fake)

``run`` and ``step`` are re-exported as *modules* for exactly that reason, using
the redundant-alias form (``from . import run as run``) rather than an ``__all__``
entry: ``__all__`` is asserted against by
``test_install_selects_lives_in_the_selects_package`` and stays exactly as it was
before the split.

The private helpers below (``_carries_positional_index``, ``_compile_step``,
``_short``, ``_target_desc``, ``_wait_for_new_pages``, ``_warn_absent``) look like
they break the withholding rule — tests reach them through this facade. They do
not: none of them is a seam, because nothing patches them. Nor can one quietly
become one, which is what makes leaving them re-exported safe — the moment a test
writes ``monkeypatch.setattr(compile_module, "_short", ...)`` the seam scan picks
the name up and ``test_facade_withholds_every_patched_name`` fails on this very
re-export, before the dead patch can leave anything green.
"""

from __future__ import annotations

from guidebot_recorder.resolver.resolution import heuristic_expect

from . import run as run
from . import step as step
from .cache import (
    _carries_positional_index as _carries_positional_index,
)
from .cache import (
    compile_up_to_date,
    needs_positional_recheck,
)
from .describe import (
    _short as _short,
)
from .describe import (
    _target_desc as _target_desc,
)
from .describe import (
    _warn_absent as _warn_absent,
)
from .pages import (
    _wait_for_new_pages as _wait_for_new_pages,
)
from .run import run_compile, run_compile_in_browser
from .step import (
    _compile_step as _compile_step,
)

__all__ = [
    "compile_up_to_date",
    "heuristic_expect",
    "needs_positional_recheck",
    "run_compile",
    "run_compile_in_browser",
]
