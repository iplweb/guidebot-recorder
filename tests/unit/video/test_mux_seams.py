"""Guard: every ``video.mux`` test seam must still reach the consumer it patches.

Splitting ``mux.py`` into a package moved every consumer into a module of its own.
A consumer that writes ``from .ffmpeg import _run`` binds the function object at
import time, so ``monkeypatch.setattr(mux_module.ffmpeg, "_run", fake)`` rebinds a
name nobody reads any more. The patch reaches nobody and the test keeps passing
while checking nothing — ``detect_content_crop`` degrades to ``None`` on *any*
failure, so its assertion still holds and the run quietly shells out to real
ffmpeg. The only defence is late binding: reach a seam through the module object.

The invariant, stated so it cannot be read two ways:

    the module a test patches must be the module whose globals the consumer
    reads at call time.

That makes the two halves a coupled pair, chosen per name: a consumer that keeps
``from X import name`` must be patched *on X's importer*, a consumer rewired to
``mod.name(...)`` must be patched *on mod*. Mixing them misses silently. This
package picks the second form for all three of its seams, inside the package and
out (``video/timeline.py``, ``video/sfx.py``, ``video/audiobed.py``,
``recorder/render.py``), so one patch line covers every consumer of a name.

``from X import name`` is only the most obvious way to snapshot a seam's value.
``alias = X.name`` at module level and ``def f(..., _name=X.name)`` do the same
thing with no import statement in sight, so the scan below covers all three.

**There is no allowlist, and zero is the right number of entries.** A "harmless"
re-export is a laundering channel: a module that name-imports a seam becomes
itself an exporter of an early-bound copy, and every consumer that goes through
the re-export is invisible to a patch on the defining module *and* to a scan that
filters on the substring "mux". The one entry this file used to carry
(``guidebot_recorder.video`` re-exporting ``probe_duration``) bought exactly that
hole and had no callers; it is gone.

**The seam list is discovered, not written down here.** It is read out of
``tests/`` by finding what is actually patched on the mux package, so a seam added
tomorrow is covered the day it appears — a hardcoded list would rot exactly when
it starts mattering. Needs no ffmpeg: this only parses source.
"""

from __future__ import annotations

import ast
import importlib
from collections import defaultdict
from pathlib import Path

PACKAGE_NAME = "guidebot_recorder.video.mux"

# ``import guidebot_recorder.video.mux as mux_module`` would bind the *function*
# ``mux``: ``video/__init__.py`` re-exports it and that shadows the submodule
# attribute on the parent package. Pre-existing, and the reason test_mux.py
# reaches for importlib too.
mux_module = importlib.import_module(PACKAGE_NAME)

PACKAGE = Path(mux_module.__file__).parent
SOURCE_ROOT = PACKAGE.parents[1]
TESTS_ROOT = SOURCE_ROOT.parent / "tests"


def _sources(root: Path) -> list[Path]:
    paths = sorted(root.rglob("*.py"))
    assert paths, f"no modules found under {root}"
    return paths


def _package_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the mux package by ``importlib.import_module`` in one file."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
            continue
        args = node.value.args
        if not args or not isinstance(args[0], ast.Constant) or args[0].value != PACKAGE_NAME:
            continue
        aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return aliases


def _discover_seams() -> dict[str, set[str]]:
    """Patched name -> the target expressions tests patch it on."""
    seams: dict[str, set[str]] = defaultdict(set)
    for path in _sources(TESTS_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = _package_aliases(tree)
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"):
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                continue
            name = node.args[1].value
            if not isinstance(name, str):
                continue
            root = node.args[0]
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in aliases:
                seams[name].add(ast.unparse(node.args[0]))
    return dict(seams)


SEAMS = _discover_seams()


def test_the_scan_found_the_seams() -> None:
    # A scan that silently matches nothing would make every assertion below
    # vacuously true, which is the same failure mode the guard exists to catch.
    assert SEAMS, f"no monkeypatch targets found on {PACKAGE_NAME} under {TESTS_ROOT}"


def test_every_patch_targets_a_submodule_not_the_facade() -> None:
    # Patching the facade cannot work: the consumer lives in a submodule and reads
    # that submodule's globals. Requiring `<alias>.<submodule>` keeps the patch and
    # the consumer on the same module object.
    offenders: list[str] = []
    for name, targets in sorted(SEAMS.items()):
        for target in sorted(targets):
            _, _, owner = target.partition(".")
            if not owner or "." in owner:
                offenders.append(
                    f"{name!r} is patched on {target!r}; patch it on the submodule "
                    f"that defines it, so the consumer's globals are the ones rebound"
                )
                continue
            submodule = getattr(mux_module, owner, None)
            if submodule is None or not hasattr(submodule, name):
                offenders.append(f"{name!r} is patched on {target!r}, which does not define it")
    assert not offenders, "\n".join(offenders)


def test_facade_withholds_every_patched_name() -> None:
    # Re-exporting a seam would let a patch on the facade succeed and reach
    # nobody. Withholding turns the same mistake into an AttributeError.
    for name in sorted(SEAMS):
        assert not hasattr(mux_module, name), (
            f"the facade re-exports the seam {name!r}; a patch on the facade would "
            f"silently reach nobody — import the defining submodule instead"
        )
        assert name not in mux_module.__all__


def _seam_attribute(node: ast.expr | None) -> str | None:
    """The seam name in an ``<anything>.<seam>`` expression, else ``None``."""
    return node.attr if isinstance(node, ast.Attribute) and node.attr in SEAMS else None


def _import_offenders(node: ast.ImportFrom, where: str, *, inside_package: bool) -> list[str]:
    """``from X import seam`` — the value is copied into this module's globals."""
    # Inside the package the imports are relative (``from .ffmpeg import``), so
    # ``node.module`` is a bare submodule name and no "mux" appears in it —
    # filtering on the substring here would pass every in-package violation,
    # which is the case this guard exists for.
    if not inside_package and "mux" not in (node.module or ""):
        return []
    return [
        f"{where}:{node.lineno} imports the seam {alias.name!r} by name; a patch on "
        f"the defining module would not reach it. Import the module instead and call "
        f"it as an attribute"
        for alias in node.names
        if alias.name in SEAMS
    ]


def _alias_offenders(node: ast.Assign | ast.AnnAssign, where: str) -> list[str]:
    """``alias = X.seam`` — bound once, at import time."""
    # No module filter here and none possible: ``_run = ffmpeg._run`` names no
    # module the AST can resolve. The attribute name alone is the signal, and a
    # false positive is a rename away.
    seam = _seam_attribute(node.value)
    if seam is None:
        return []
    return [
        f"{where}:{node.lineno} aliases the seam {seam!r} into a module-level name; "
        f"the alias is bound at import time, so a patch on the defining module would "
        f"not reach it. Keep the attribute access at the call site"
    ]


def _default_offenders(node: ast.arguments, where: str) -> list[str]:
    """``def f(..., _seam=X.seam)`` — a default is evaluated once, at def time."""
    return [
        f"{where}:{default.lineno} binds the seam {seam!r} as a parameter default; "
        f"defaults are evaluated once at def time, so a patch on the defining module "
        f"would not reach it. Read the attribute inside the body"
        for default in [*node.defaults, *node.kw_defaults]
        if (seam := _seam_attribute(default)) is not None
    ]


def _binding_offenders(path: Path) -> list[str]:
    """Every import-time snapshot of a seam's value in one source file."""
    where = path.relative_to(SOURCE_ROOT).as_posix()
    inside_package = path.is_relative_to(PACKAGE)
    offenders: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            offenders += _import_offenders(node, where, inside_package=inside_package)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            offenders += _alias_offenders(node, where)
        elif isinstance(node, ast.arguments):
            offenders += _default_offenders(node, where)
    return offenders


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into another module's globals, all
    # equally fatal and all invisible to a patch on the defining module. One
    # helper per form, above — the guard is itself held to the complexity limit
    # it exists to help enforce.
    offenders = [o for path in _sources(SOURCE_ROOT) for o in _binding_offenders(path)]
    assert not offenders, "\n".join(offenders)
