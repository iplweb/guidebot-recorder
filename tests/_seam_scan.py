"""What the source *says*: module aliases, patch sites, and per-file facts.

This module answers one question and takes no view on the answer: **given a file,
what does it bind, patch, define and import?** Whether any of that is *allowed* is
:mod:`tests._seam_guard`'s business, and keeping the two apart is what lets the
same scan serve packages whose rules are opposites.

**Discovery must recognise every binding idiom, not one.** The guards for
``video.mux`` and ``recorder.compile`` only ever looked for
``importlib.import_module("<dotted>")``, because that was the single idiom their
own tests happened to use. ``tests/`` binds modules four ways, and
``recorder.render`` — phase 1c, the largest patch surface in the repo — uses the
other three exclusively::

    import guidebot_recorder.recorder.render as R                  # 26x
    from guidebot_recorder.recorder import render as render_module #  1x
    monkeypatch.setattr("guidebot_recorder.recorder.render.x", f)  #  4x
    render_module = importlib.import_module("...render")           #  0x

A scan that knows only the fourth form discovers **zero** of render's sites, so
:func:`module_aliases` covers the first three and :func:`patch_sites` resolves the
fourth, which needs no name at all.

**Every setattr is classified, never filtered.** A scan that drops what it does not
understand cannot tell "there was nothing here" from "I could not read this", and
the guard downstream would report the first while meaning the second. So every
target gets a :data:`~PatchSite.form` — :data:`ALIAS_FORM`, :data:`DOTTED_FORM` or
:data:`UNKNOWN_FORM` — and an :data:`~PatchSite.evidence` string, and the decision
about what to do with an unreadable one is made where the rules live.

Parses source and nothing else: no imports of the code under test, no ffmpeg, no
browser, no network.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: How a ``monkeypatch.setattr`` target was resolved. ``"unknown"`` means the scan
#: could not attribute the target to a module at all.
ALIAS_FORM = "alias"
DOTTED_FORM = "dotted-string"
UNKNOWN_FORM = "unknown"


# --------------------------------------------------------------------------- #
# module aliases: the four ways tests bind a module to a name
# --------------------------------------------------------------------------- #


def absolute_module(node: ast.ImportFrom, home_package: str) -> str:
    """The dotted module an ``ImportFrom`` names, with ``.``-relative forms resolved."""
    if not node.level:
        return node.module or ""
    parts = home_package.split(".") if home_package else []
    base = parts[: len(parts) - node.level + 1]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _plain_import_aliases(node: ast.Import) -> dict[str, str]:
    """``import a.b.c`` -> ``{"a": "a"}``; ``import a.b.c as d`` -> ``{"d": "a.b.c"}``."""
    aliases: dict[str, str] = {}
    for alias in node.names:
        head = alias.name.split(".")[0]
        aliases[alias.asname or head] = alias.name if alias.asname else head
    return aliases


def _from_import_aliases(node: ast.ImportFrom, home_package: str) -> dict[str, str]:
    """``from a.b import c as d`` -> ``{"d": "a.b.c"}`` (``c`` may be a module or a name)."""
    module = absolute_module(node, home_package)
    prefix = f"{module}." if module else ""
    return {alias.asname or alias.name: f"{prefix}{alias.name}" for alias in node.names}


def _importlib_aliases(node: ast.Assign) -> dict[str, str]:
    """``d = importlib.import_module("a.b.c")`` -> ``{"d": "a.b.c"}``."""
    func = node.value.func if isinstance(node.value, ast.Call) else None
    if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
        return {}
    args = node.value.args  # type: ignore[union-attr]
    if not args or not isinstance(args[0], ast.Constant) or not isinstance(args[0].value, str):
        return {}
    return {t.id: args[0].value for t in node.targets if isinstance(t, ast.Name)}


def module_aliases(tree: ast.AST, home_package: str = "") -> dict[str, str]:
    """Every name in one file that is bound to a dotted module path.

    Covers three of the four discovery forms; the fourth (a dotted string handed
    straight to ``monkeypatch.setattr``) needs no name and is resolved in
    :func:`patch_sites`.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= _plain_import_aliases(node)
        elif isinstance(node, ast.ImportFrom):
            aliases |= _from_import_aliases(node, home_package)
        elif isinstance(node, ast.Assign):
            aliases |= _importlib_aliases(node)
    return aliases


# --------------------------------------------------------------------------- #
# patch sites: every monkeypatch.setattr under tests/, classified
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PatchSite:
    """One ``monkeypatch.setattr`` call, resolved as far as the source allows."""

    where: str
    """``tests/unit/video/test_mux.py:366``."""
    scope: str
    """Name of the enclosing top-level ``def``/``class``, or ``"<module>"``."""
    text: str
    """Source text of the target expression, or the dotted string."""
    form: str
    """One of :data:`ALIAS_FORM`, :data:`DOTTED_FORM`, :data:`UNKNOWN_FORM`."""
    path: str | None
    """Dotted module path the target resolves to, or ``None`` when unresolved."""
    attr: str
    """The attribute being replaced."""
    evidence: str
    """``text`` plus the source of every statement that binds its root name."""


def _scoped_nodes(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Every node, tagged with the top-level definition it lives in."""
    for top in tree.body:
        scope = getattr(top, "name", "<module>")
        for node in ast.walk(top):
            yield scope, node


def _root_name(node: ast.expr) -> str | None:
    """The leftmost ``Name`` of an ``a.b.c`` chain."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bindings(tree: ast.AST) -> dict[str, list[str]]:
    """Name -> the source of every assignment that binds it.

    A target the alias scan cannot resolve is usually a runtime object
    (``monkeypatch.setattr(page, "goto", ...)``). Its assignment is the only place
    left where the guarded package can still be mentioned — ``rm =
    sys.modules["...render"]`` launders a module through a form no alias rule
    recognises — so the binding text joins the evidence a completeness failure
    is judged on.
    """
    bound: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id].append(ast.unparse(node))
    return dict(bound)


def _setattr_args(node: ast.AST) -> tuple[ast.expr, str] | None:
    """``(target expression, attribute name)`` for a ``*.setattr(...)`` call.

    Handles both pytest forms: ``setattr(obj, "name", value)`` and the two-argument
    ``setattr("dotted.path.name", value)``.
    """
    if not isinstance(node, ast.Call):
        return None
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first, first.value.rpartition(".")[2]
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        name = node.args[1].value
        return (first, name) if isinstance(name, str) else None
    return None


def _resolve(target: ast.expr, aliases: dict[str, str]) -> tuple[str, str | None]:
    """``(form, dotted module path)`` for one ``setattr`` target expression."""
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return DOTTED_FORM, target.value.rpartition(".")[0]
    root = _root_name(target)
    if root is None or root not in aliases:
        return UNKNOWN_FORM, None
    suffix = ast.unparse(target).partition(".")[2]
    head = aliases[root]
    return ALIAS_FORM, f"{head}.{suffix}" if suffix else head


def patch_sites(tree: ast.Module, aliases: dict[str, str], relative: str) -> list[PatchSite]:
    """Every ``monkeypatch.setattr`` in one test file, resolved and classified."""
    bound = _bindings(tree)
    sites: list[PatchSite] = []
    for scope, node in _scoped_nodes(tree):
        found = _setattr_args(node)
        if found is None:
            continue
        target, attr = found
        form, path = _resolve(target, aliases)
        text = ast.unparse(target)
        evidence = " ".join([text, *bound.get(_root_name(target) or "", [])])
        where = f"{relative}:{node.lineno}"  # type: ignore[attr-defined]
        sites.append(PatchSite(where, scope, text, form, path, attr, evidence))
    return sites


# --------------------------------------------------------------------------- #
# source facts: what each module defines, and what it binds early
# --------------------------------------------------------------------------- #


def defined_names(tree: ast.Module) -> set[str]:
    """Module-level names a file *defines* (not ones it imports or aliases).

    ``alias = other.name`` is deliberately excluded: it binds a value, it does not
    define one, and treating it as a definition would let a laundering alias
    disguise itself as the seam's home.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign) and not isinstance(node.value, ast.Attribute | ast.Name):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def name_imported(tree: ast.Module) -> set[str]:
    """Names a file pulls in with ``from X import name`` — bound at import time."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


@dataclass(frozen=True)
class SourceFile:
    """One parsed module under the source root."""

    path: Path
    relative: str
    tree: ast.Module
    home: str
    """The package submodule this file *is* (``"probe"``), or ``""`` when outside."""
    home_package: str
    """The dotted package the file lives in, for resolving relative imports."""


def read_source(path: Path, source_root: Path, package_dir: Path) -> SourceFile:
    """Parse one file and record where it sits relative to the guarded package."""
    tree = ast.parse(path.read_text(), filename=str(path))
    home = path.stem if path.is_relative_to(package_dir) else ""
    home_package = ".".join(path.parent.relative_to(source_root.parent).parts)
    return SourceFile(path, path.relative_to(source_root).as_posix(), tree, home, home_package)


def source_paths(root: Path) -> list[Path]:
    """Every ``.py`` under ``root``. Empty is an error, not an empty scan."""
    paths = sorted(root.rglob("*.py"))
    assert paths, f"no modules found under {root}"
    return paths
