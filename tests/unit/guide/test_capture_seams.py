"""Guard: every name the guide tests patch on ``capture`` is still called there.

``guide/capture.py`` was one 500-line module. Splitting the phases out into
``guide/replay.py`` created a failure mode the file boundary itself cannot
prevent, and it is the quiet kind. Tests patch three names on the ``capture``
module object::

    monkeypatch.setattr(capture, "reuse_failure", _async_none)

``monkeypatch.setattr`` only checks that the attribute *exists*. Move
``_reject_unusable_target`` into ``replay.py`` and the name still exists on
``capture`` — the ``from ... import reuse_failure`` line is still there — while
the consumer now resolves ``reuse_failure`` from ``replay``'s globals. Every one
of those 39 patches would succeed, reach nobody, and leave the suite green while
it asserted nothing about the code it thought it was stubbing.

The invariant, stated so it cannot be read two ways:

    the module a test patches must be the module whose globals the consumer
    reads at call time.

This file checks the one direction that applies here — ``capture.py`` must still
*call* each patched name, and no sibling in ``guide/`` may hold a second copy of
it. That second half matters because a second early binding is a second copy: a
patch on ``capture`` covers one of them and the other interception path
disappears with no test turning red.

Deliberately **not** built on :class:`tests._seam_guard.SeamGuard`. That helper
guards *packages*, where the answer is "which submodule", and it would report
every patch here as landing on a facade. ``capture`` is a plain module and stays
one; the seam list, however, is discovered the same way — read out of ``tests/``
rather than written down, so a seam added tomorrow is covered the day it appears.

Needs no browser and imports nothing under test: this only parses source.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._seam_scan import UNKNOWN_FORM, module_aliases, patch_sites, source_paths

MODULE = "guidebot_recorder.guide.capture"

_TESTS_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _TESTS_ROOT.parent / "guidebot_recorder"
_GUIDE_DIR = _SOURCE_ROOT / "guide"
_CAPTURE = _GUIDE_DIR / "capture.py"


def _sites() -> list:
    """Every ``monkeypatch.setattr`` under ``tests/``, resolved to a module."""

    found = []
    for path in source_paths(_TESTS_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        home_package = ".".join(path.parent.relative_to(_TESTS_ROOT.parent).parts)
        relative = path.relative_to(_TESTS_ROOT.parent).as_posix()
        found += patch_sites(tree, module_aliases(tree, home_package), relative)
    return found


SITES = _sites()
#: Names the suite substitutes on the ``capture`` module — the seam list.
SEAMS = sorted({site.attr for site in SITES if site.path == MODULE})


def _called_names(tree: ast.Module) -> set[str]:
    """Bare names this module *calls*: ``f(...)``, not ``obj.f(...)``."""

    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_the_scan_found_the_seams() -> None:
    # Non-emptiness is not liveness on its own, so the completeness half comes
    # first: a target the scan cannot attribute to any module, whose text or
    # binding statement mentions `capture`, means the scan has gone blind and
    # every assertion below is vacuously true.
    blind = [
        f"{site.where} patches {site.attr!r} on {site.text!r}, which the scan cannot "
        f"attribute to any module — and it mentions 'capture'"
        for site in SITES
        if site.form == UNKNOWN_FORM and "capture" in site.evidence
    ]
    assert not blind, "\n".join(blind)
    assert SEAMS, f"no monkeypatch targets found on {MODULE} under {_TESTS_ROOT}"


def test_capture_still_calls_every_name_the_tests_patch_on_it() -> None:
    # The check the split exists to survive. A seam nobody calls from this module
    # is a patch that reaches nobody: the consumer moved out and took its globals
    # with it. Ruff's F401 catches only the case where the import went unused too.
    called = _called_names(ast.parse(_CAPTURE.read_text(), filename=str(_CAPTURE)))
    orphans = [
        f"{name!r} is patched on {MODULE} but guide/capture.py never calls it; the "
        f"consumer has moved and reads another module's globals, so the patch "
        f"succeeds and intercepts nothing. Move the consumer back, or move the "
        f"patch to the module that now calls it"
        for name in SEAMS
        if name not in called
    ]
    assert not orphans, "\n".join(orphans)


def test_no_sibling_module_holds_a_second_copy_of_a_seam() -> None:
    # A second `from X import seam` anywhere in `guide/` is a second early-bound
    # copy. One patch line covers `capture`'s and the other path is intercepted by
    # nothing, silently.
    offenders = []
    for path in sorted(_GUIDE_DIR.glob("*.py")):
        if path == _CAPTURE:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            offenders += [
                f"guide/{path.name}:{node.lineno} imports the seam {alias.name!r} by "
                f"name; the patch lands on {MODULE} and would not reach this copy"
                for alias in node.names
                if (alias.asname or alias.name) in SEAMS
            ]
    assert not offenders, "\n".join(offenders)
