"""Guard: every ``recorder.compile`` test seam must still reach the consumer it patches.

Splitting ``compile.py`` into a package moved every consumer into a module of its
own. The two seams are names the package pulls in from elsewhere and the tests
substitute: ``write_compiled`` (read by the ``checkpoint`` closure inside
``run_compile``) and ``resolve_step_target`` (read by ``_compile_step``). Before
the split both lived in one module's globals and one patch line covered them; now
they live in ``run.py`` and ``step.py``, and a patch aimed at the facade rebinds a
name nobody reads any more.

The invariant, stated so it cannot be read two ways:

    the module a test patches must be the module whose globals the consumer
    reads at call time.

That makes the two halves a coupled pair, chosen per name: a consumer that keeps
``from X import name`` must be patched *on the consumer's module*, a consumer
rewired to ``mod.name(...)`` must be patched *on mod*. Mixing them misses
silently. This package picks the first form for both seams — the defining modules
(``scenario.compiled``, ``resolver.resolution``) are outside the package and have
consumers of their own that these patches must not disturb — so the name-import
in ``run.py`` and ``step.py`` is not a violation, it *is* the seam. What the scan
below forbids is a *second* binding: any other module in the package that pulls
the same name in becomes a second copy, and one patch line would then cover only
one of them.

Why that matters more than it looks: both seams sit on paths whose assertions
survive a dead patch. ``test_recompile_reuses_cache_without_rewriting_unchanged_sidecar``
counts calls through a wrapper — a patch that reaches nobody leaves the counter at
zero and the test would have to be read closely to notice it is now asserting
about nothing. The only defence is to keep the patch and the consumer on the same
module object.

``from X import name`` is only the most obvious way to snapshot a seam's value.
``alias = X.name`` at module level and ``def f(..., _name=X.name)`` do the same
thing with no import statement in sight, so the scan below covers all three.

**There is no allowlist, and the only permitted binding is the patch target's
own.** A "harmless" re-export is a laundering channel: a module that name-imports
a seam becomes itself an exporter of an early-bound copy, and every consumer that
goes through the re-export is invisible to a patch on the module the test aims at.

**The seam list is discovered, not written down here.** It is read out of
``tests/`` by finding what is actually patched on the compile package, so a seam
added tomorrow is covered the day it appears — a hardcoded list would rot exactly
when it starts mattering. Needs no browser: this only parses source.
"""

from __future__ import annotations

import ast
import importlib
from collections import defaultdict
from pathlib import Path

PACKAGE_NAME = "guidebot_recorder.recorder.compile"

compile_module = importlib.import_module(PACKAGE_NAME)

PACKAGE = Path(compile_module.__file__).parent
SOURCE_ROOT = PACKAGE.parents[1]
TESTS_ROOT = SOURCE_ROOT.parent / "tests"


def _sources(root: Path) -> list[Path]:
    paths = sorted(root.rglob("*.py"))
    assert paths, f"no modules found under {root}"
    return paths


def _direct_aliases(node: ast.Import) -> set[str]:
    """Names bound by ``import guidebot_recorder.recorder.compile as alias``."""
    return {alias.asname for alias in node.names if alias.name == PACKAGE_NAME and alias.asname}


def _importlib_aliases(node: ast.Assign) -> set[str]:
    """Names bound by ``alias = importlib.import_module("...compile")``."""
    func = node.value.func if isinstance(node.value, ast.Call) else None
    if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
        return set()
    args = node.value.args
    if not args or not isinstance(args[0], ast.Constant) or args[0].value != PACKAGE_NAME:
        return set()
    return {target.id for target in node.targets if isinstance(target, ast.Name)}


def _package_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the compile package in one file, by either import form."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= _direct_aliases(node)
        elif isinstance(node, ast.Assign):
            aliases |= _importlib_aliases(node)
    return aliases


def _setattr_calls(tree: ast.AST) -> list[tuple[ast.expr, str]]:
    """``(target expression, attribute name)`` for every ``setattr(obj, "name", ...)``."""
    calls: list[tuple[ast.expr, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            continue
        if isinstance(node.args[1].value, str):
            calls.append((node.args[0], node.args[1].value))
    return calls


def _rooted_in(target: ast.expr, aliases: set[str]) -> bool:
    """Does ``target`` walk back to one of the package aliases?"""
    while isinstance(target, ast.Attribute):
        target = target.value
    return isinstance(target, ast.Name) and target.id in aliases


def _discover_seams() -> dict[str, set[str]]:
    """Patched name -> the target expressions tests patch it on."""
    seams: dict[str, set[str]] = defaultdict(set)
    for path in _sources(TESTS_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = _package_aliases(tree)
        if not aliases:
            continue
        for target, name in _setattr_calls(tree):
            if _rooted_in(target, aliases):
                seams[name].add(ast.unparse(target))
    return dict(seams)


SEAMS = _discover_seams()

#: Seam name -> the submodules tests patch it on (``compile_module.run`` -> ``run``).
#: Those, and only those, may bind the name at import time.
PATCH_TARGETS = {
    name: {target.partition(".")[2] for target in targets} for name, targets in SEAMS.items()
}


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
                    f"whose globals the consumer reads, so the patch and the call "
                    f"site land on the same module object"
                )
                continue
            submodule = getattr(compile_module, owner, None)
            if submodule is None or not hasattr(submodule, name):
                offenders.append(f"{name!r} is patched on {target!r}, which does not define it")
    assert not offenders, "\n".join(offenders)


def test_facade_withholds_every_patched_name() -> None:
    # Re-exporting a seam would let a patch on the facade succeed and reach
    # nobody. Withholding turns the same mistake into an AttributeError.
    for name in sorted(SEAMS):
        assert not hasattr(compile_module, name), (
            f"the facade re-exports the seam {name!r}; a patch on the facade would "
            f"silently reach nobody — patch the consuming submodule instead"
        )
        assert name not in compile_module.__all__


def _misbound(name: str, home: str) -> bool:
    """Is ``name`` a seam that module ``home`` has no business binding early?

    ``home`` is the package submodule the source file belongs to, or ``""`` for a
    file outside the package. Only the submodules tests actually patch a seam on
    may bind it; every other early binding is a second copy one patch cannot cover.
    """
    return name in SEAMS and home not in PATCH_TARGETS.get(name, set())


def _seam_attribute(node: ast.expr | None) -> str | None:
    """The seam name in an ``<anything>.<seam>`` expression, else ``None``."""
    return node.attr if isinstance(node, ast.Attribute) and node.attr in SEAMS else None


def _import_offenders(relative: str, tree: ast.AST, home: str) -> list[str]:
    """``from X import seam`` in a module that is not the seam's patch target."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # Outside the package, only a route *through* the package can launder a
        # seam: `from guidebot_recorder.scenario.compiled import write_compiled`
        # in render.py is render's own business and its own patch target.
        if not home and not (node.module or "").startswith(PACKAGE_NAME):
            continue
        offenders += [
            f"{relative}:{node.lineno} imports the seam {alias.name!r} by name; the "
            f"patch lands on {sorted(PATCH_TARGETS[alias.name])} and would not reach "
            f"this copy. Call it through the module that is patched"
            for alias in node.names
            if _misbound(alias.name, home)
        ]
    return offenders


def _alias_offenders(relative: str, tree: ast.AST, home: str) -> list[str]:
    """``alias = X.seam`` at module level — an import-time snapshot with no import."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        seam = _seam_attribute(node.value)
        if seam is not None and _misbound(seam, home):
            offenders.append(
                f"{relative}:{node.lineno} aliases the seam {seam!r} into a module-level "
                f"name; the alias is bound at import time, so the patch on "
                f"{sorted(PATCH_TARGETS[seam])} would not reach it. Keep the attribute "
                f"access at the call site"
            )
    return offenders


def _default_offenders(relative: str, tree: ast.AST, home: str) -> list[str]:
    """``def f(..., _seam=X.seam)`` — a default is evaluated once, at def time."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.arguments):
            continue
        for default in [*node.defaults, *node.kw_defaults]:
            seam = _seam_attribute(default)
            if seam is not None and _misbound(seam, home):
                offenders.append(
                    f"{relative}:{default.lineno} binds the seam {seam!r} as a parameter "
                    f"default; defaults are evaluated once at def time, so the patch on "
                    f"{sorted(PATCH_TARGETS[seam])} would not reach it. Read the "
                    f"attribute inside the body"
                )
    return offenders


def test_no_seam_is_bound_at_import_time() -> None:
    # Three ways to snapshot a seam's *value* into the wrong module's globals, all
    # equally fatal and all invisible to the patch the tests actually write.
    offenders: list[str] = []
    for path in _sources(SOURCE_ROOT):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        home = path.stem if path.is_relative_to(PACKAGE) else ""
        tree = ast.parse(path.read_text(), filename=str(path))
        for find in (_import_offenders, _alias_offenders, _default_offenders):
            offenders += find(relative, tree, home)
    assert not offenders, "\n".join(offenders)
