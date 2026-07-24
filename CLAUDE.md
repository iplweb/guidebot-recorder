# CLAUDE.md — guidebot-recorder

Guidance for anyone (human or AI) editing this repo. It records the **non-obvious**
rules a well-meaning "simplification" would otherwise delete — reintroducing the
exact bugs a large 2026-07 cleanup removed. Read the rationale in full at
`docs/superpowers/specs/2026-07-22-code-cleanup-design.md` (and its executing
prompt `docs/superpowers/specs/2026-07-22-code-cleanup-prompt.md`).

## Two hard limits — machine-enforced, no exceptions

1. **Every `.py` file ≤ 600 lines** — application code and tests alike.
2. **Every function cyclomatic complexity ≤ 10.**

Both are checked in CI and in pre-commit, so a regression is *unmergeable*, not
merely discouraged:

- CC is ruff's `C901` (`select` in `[tool.ruff.lint]`) with
  `[tool.ruff.lint.mccabe] max-complexity = 10`. It runs as `ruff check .` over the
  **whole repo including `tests/`** — a file scoped only to `guidebot_recorder/`
  once hid a 16th violation sitting in a test file.
- File length is the local pre-commit hook `check-file-length`
  (`scripts/check_file_length.py`, stdlib only). Its `files:` regex covers both
  `guidebot_recorder/` and `tests/`.

**Forbidden ways to "pass":** `# noqa: C901`; raising `max-complexity`; adding a
`C901` entry to `per-file-ignores`; a per-file allowlist in the length hook; a
one-line helper called from exactly one place purely to move a branch. If a
function genuinely cannot go below 10 without hurting readability, raise it as a
design question — do not silence it.

**`match` does not reduce cyclomatic complexity in ruff.** Three `case` cost the
same as three `elif` (each `if`/`elif`/`for`/`while`/`except`/`case` is +1;
`else`/`with`/ternary/comprehension/`and`/`or` are 0). Rewriting an `if/elif`
ladder into `match` to beat the metric is wasted work. A **nested `def` adds its
own full CC to the parent** — moving a closure onto an object is the cheapest real
reduction.

## The facade + seam pattern — do not "simplify" it

Several packages have an `__init__.py` that is a **facade holding no logic**:

- `guidebot_recorder/video/mux/`
- `guidebot_recorder/recorder/compile/`
- `guidebot_recorder/recorder/render/`
- `guidebot_recorder/recorder/select/`

The facade re-exports the names other code and tests **import**, so splitting a
module into a package kept `from guidebot_recorder.video.mux import ...` working.
But it **deliberately withholds from re-export any name that tests monkeypatch**
(the package's "seams" — e.g. `_run`, `_run_to_output`, `probe_duration` in
`mux`; `write_compiled`, `resolve_step_target` in `compile`).

**Why withholding matters — this is the whole point.** A test patches a seam with
`monkeypatch.setattr`. The real consumer lives in a submodule and resolves the name
from *its own* globals. If the facade re-exported the seam, a patch aimed at the
facade would *succeed* yet reach nobody — a silently dead patch that leaves the test
green while it asserts nothing (the worst failures this cleanup found: a test
patching a timeout to 0.3s, asserting `elapsed < 5.0`, still "passing" after the
patch broke — guarding nothing). Withholding turns that same mistake into an
immediate `AttributeError`/`ImportError` at collection time, naming the attribute.

Therefore:

- **Never re-export a withheld seam "for convenience."** That is a regression, not
  a cleanup. It reintroduces silent test failures.
- **Consumers call seams through the module object**, never by value:
  `from . import ffmpeg` then `ffmpeg._run(...)` — **not** `from .ffmpeg import _run`
  then `_run(...)` (the latter binds at import time; no patch reaches it). This holds
  for production consumers too when the name is a seam.
- Some seams are **defined outside** their package (`compile`'s `write_compiled`
  comes from `scenario.compiled`). There the consumer keeps `from X import name` and
  the test patches the *consuming* submodule — the name-import **is** the seam, not a
  violation. Which module to patch depends on the definition site; see the guard.

**AST guard tests enforce all of this** — do not delete or weaken them:
`tests/_seam_guard.py` (the invariant as executable rules, plus `tests/_seam_scan.py`)
and the per-package `tests/**/test_*_seams.py`. The seam list is **discovered from
`tests/` by scanning**, never hardcoded — so a hardcoded seam list is itself a
regression (it rots exactly when a new seam appears). The guard also checks that a
name-imported seam with two consumers is patched on *both*.

## Tests: no `conftest.py`, helpers are explicit

**`tests/` has no `conftest.py` by design** (decision D4). Shared scaffolding lives
in explicitly-imported `_*_helpers.py` files, so reading one test file shows you
everything it uses. Do not add a `conftest.py`.

A shared browser-`page` fixture is exported as a **factory** (`make_page`), not as a
pytest fixture — because a fixture whose name is also a test parameter is a
redefinition ruff flags as **F811** (~37–70 times over). Re-exporting an actual
fixture across split files needs `# noqa: F401` (ruff runs `F` and `tests/**` only
ignores `E501`). `pytestmark` does **not** inherit through a helper import: each new
test file must carry its own markers/`skipif` literally (export the marker block as a
constant and write `pytestmark = FFMPEG`).

## Scope reminder

Both gates cover `tests/` as well as `guidebot_recorder/`: the C901 gate because it
is `ruff check .` over the whole tree, and the length hook because its `files:` regex
includes `tests/`. Keep it that way.
