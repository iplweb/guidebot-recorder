"""Whether the source is *allowed*: the seam invariant, as executable rules.

A *seam* is a name a test substitutes with ``monkeypatch.setattr`` so a call never
reaches the real thing. Splitting a module into a package moves consumers into
modules of their own, and a consumer that wrote ``from X import name`` bound the
value at import time — a patch aimed at any other module rebinds a name nobody
reads. The patch reaches nobody and the test keeps passing while checking nothing.

The invariant, stated so it cannot be read two ways:

    the module a test patches must be the module whose globals the consumer
    reads at call time.

That makes the two halves a **coupled pair, chosen per name**:

    consumer keeps ``from X import name``  -> test patches the CONSUMER's module
    consumer rewired to ``mod.name(...)``  -> test patches the DEFINING module

Mixing them misses silently, which is why this module never asks "is there a
name-import?" without first asking "where is this name defined?".

Two packages already ship a guard and they disagreed, because the packages differ:

``video.mux``
    All three seams (``_run``, ``_run_to_output``, ``probe_duration``) are defined
    **inside** the package. The patch target is the defining submodule, so no
    module anywhere may name-import them.

``recorder.compile``
    Both seams (``write_compiled``, ``resolve_step_target``) are defined
    **outside** the package (``scenario.compiled``, ``resolver.resolution``). The
    consumers keep ``from X import name`` and the tests patch the *consuming*
    submodule, so the name-import in ``run.py``/``step.py`` is not a violation —
    it **is** the seam.

Both rules are the same rule once it is keyed on the definition site, and that is
what :class:`SeamGuard` implements. ``recorder.render`` (phase 1c) is dominated by
the second mode — 10 of its 19 patched names are cross-package name-imports — so a
guard hardcoding the first mode would flag dozens of *correct* patch sites and push
the tests into patching shared packages globally.

**Liveness must be completeness, not non-emptiness.** ``assert SEAMS`` is
all-or-nothing: the moment one file adopts a recognised idiom the scan goes
non-empty and the other eighty sites are silently unscanned, leaving every
assertion here vacuously true — the very failure the guard exists to catch.
:mod:`tests._seam_scan` therefore hands over *every* ``monkeypatch.setattr`` under
``tests/`` with a form attached rather than a filtered subset, and
:meth:`SeamGuard.assert_scan_complete` treats an unreadable target that mentions
the guarded package as an error. "We could not classify this" is never a silent
skip.

Imports the package under guard to read its runtime attributes; runs none of its
code. Everything else comes from the AST, so no ffmpeg and no browser.
"""

from __future__ import annotations

import ast
import importlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import ModuleType

from tests._seam_scan import (
    ALIAS_FORM,
    DOTTED_FORM,
    UNKNOWN_FORM,
    PatchSite,
    SourceFile,
    absolute_module,
    defined_names,
    module_aliases,
    name_imported,
    patch_sites,
    read_source,
    source_paths,
)

#: The owner string for a patch aimed at the package itself rather than a submodule.
FACADE = ""


@dataclass(frozen=True)
class SeamGuard:
    """Everything one package's seam guard needs, discovered from source.

    Build it with :meth:`build` and call the ``assert_*`` methods from one test
    each. Nothing here is written down by hand: the seam list is read out of
    ``tests/`` so a seam added tomorrow is covered the day it appears, and a
    hardcoded list cannot rot exactly when it starts mattering.
    """

    package: str
    module: ModuleType
    package_dir: Path
    source_root: Path
    tests_root: Path
    sites: tuple[PatchSite, ...]
    """Every ``monkeypatch.setattr`` under ``tests/`` — not only this package's."""
    files: tuple[SourceFile, ...]

    # -- construction ------------------------------------------------------- #

    @classmethod
    def build(cls, package: str) -> SeamGuard:
        """Discover everything for ``package`` from source. Imports it; runs nothing.

        ``package`` may still be a single module. ``package_dir`` is then the
        directory the split will create and simply contains no files yet, so the
        discovery half works before the split — that is how the ``render``
        simulation can measure phase 1c's patch surface today — while every
        structural check stays honest by finding no submodules.
        """
        module = importlib.import_module(package)
        origin = Path(module.__file__)  # type: ignore[arg-type]
        package_dir = origin.parent if origin.name == "__init__.py" else origin.with_suffix("")
        source_root = package_dir
        while (source_root.parent / "__init__.py").exists():
            source_root = source_root.parent
        tests_root = source_root.parent / "tests"
        sites = tuple(cls._scan_tests(tests_root))
        files = tuple(read_source(p, source_root, package_dir) for p in source_paths(source_root))
        return cls(package, module, package_dir, source_root, tests_root, sites, files)

    @staticmethod
    def _scan_tests(tests_root: Path) -> Iterator[PatchSite]:
        for path in source_paths(tests_root):
            tree = ast.parse(path.read_text(), filename=str(path))
            home_package = ".".join(path.parent.relative_to(tests_root.parent).parts)
            relative = path.relative_to(tests_root.parent).as_posix()
            yield from patch_sites(tree, module_aliases(tree, home_package), relative)

    # -- derived views ------------------------------------------------------ #

    def owner(self, site: PatchSite) -> str | None:
        """The submodule a site patches (``""`` = the facade), or ``None`` if not ours."""
        if site.path == self.package:
            return FACADE
        prefix = f"{self.package}."
        return site.path[len(prefix) :] if site.path and site.path.startswith(prefix) else None

    @cached_property
    def own_sites(self) -> list[PatchSite]:
        """Sites that patch something on this package."""
        return [site for site in self.sites if self.owner(site) is not None]

    @cached_property
    def seams(self) -> dict[str, set[str]]:
        """Patched name -> the target expressions tests patch it on."""
        found: dict[str, set[str]] = defaultdict(set)
        for site in self.own_sites:
            found[site.attr].add(site.text)
        return dict(found)

    @cached_property
    def patch_owners(self) -> dict[str, set[str]]:
        """Seam name -> the submodules tests patch it on. Only those may bind it early."""
        owners: dict[str, set[str]] = defaultdict(set)
        for site in self.own_sites:
            owners[site.attr].add(self.owner(site) or FACADE)
        return dict(owners)

    @cached_property
    def submodules(self) -> dict[str, SourceFile]:
        """Submodule stem -> its parsed source, for every file in the package."""
        return {f.home: f for f in self.files if f.home and f.home != "__init__"}

    @cached_property
    def definitions(self) -> dict[str, set[str]]:
        """Name -> the package submodules that *define* it."""
        homes: dict[str, set[str]] = defaultdict(set)
        for home, source in self.submodules.items():
            for name in defined_names(source.tree):
                homes[name].add(home)
        return dict(homes)

    @cached_property
    def consumers(self) -> dict[str, set[str]]:
        """Seam name -> package submodules that name-import it (the outside-defined mode)."""
        found: dict[str, set[str]] = defaultdict(set)
        for home, source in self.submodules.items():
            for name in name_imported(source.tree) & set(self.seams):
                found[name].add(home)
        return dict(found)

    def defined_inside(self, name: str) -> bool:
        """Is this seam defined in the package, or pulled in from outside it?"""
        return name in self.definitions

    @cached_property
    def classification(self) -> dict[str, list[PatchSite]]:
        """Every ``setattr`` under ``tests/``, bucketed by how it was resolved.

        Reporting surface for the completeness check and for sizing a split before
        it starts: ``len(guard.classification[UNKNOWN_FORM])`` is the number of
        patch sites in the whole suite that no rule can attribute to a module.
        """
        buckets: dict[str, list[PatchSite]] = {ALIAS_FORM: [], DOTTED_FORM: [], UNKNOWN_FORM: []}
        for site in self.sites:
            buckets[site.form].append(site)
        return buckets

    # -- checks ------------------------------------------------------------- #

    def completeness_offenders(self) -> list[str]:
        """Patch sites the scan cannot attribute to a module, that mention this package.

        "We could not classify this" is never a silent skip. A target whose root
        the four alias forms do not recognise is usually a runtime object and
        nobody's seam — but once its text, or the statement that bound its root
        name, mentions the guarded package, the scan has gone blind to a real
        patch and every check downstream of it is quietly weaker.
        """
        needle = self.package.rsplit(".", 1)[-1]
        return [
            f"{site.where} patches {site.attr!r} on {site.text!r}, which the scan cannot "
            f"attribute to any module — and it mentions {needle!r}. Bind the module with a "
            f"recognised form (import-as, from-import-as, importlib.import_module, or a "
            f"dotted string) so the guard can see the patch"
            for site in self.sites
            if site.form == UNKNOWN_FORM
            and (self.package in site.evidence or needle in site.evidence)
        ]

    def assert_scan_complete(self) -> None:
        """Liveness *and* completeness — a partial scan is a vacuous guard.

        Completeness is checked first on purpose. When an idiom stops being
        recognised both assertions fail, and "this target mentions the package and
        I cannot see it" names the file and the line, where "no targets found"
        only says the guard is now blind.
        """
        offenders = self.completeness_offenders()
        assert not offenders, "\n".join(offenders)
        assert self.seams, f"no monkeypatch targets found on {self.package} under {self.tests_root}"

    def _target_offender(self, name: str, owner: str, text: str) -> str | None:
        if owner == FACADE or "." in owner:
            return (
                f"{name!r} is patched on {text!r}; patch it on the submodule whose globals "
                f"the consumer reads, so the patch and the call site land on the same "
                f"module object"
            )
        if owner not in self.submodules:
            return f"{name!r} is patched on {text!r}, which is not a submodule of {self.package}"
        if not hasattr(getattr(self.module, owner, None), name):
            return f"{name!r} is patched on {text!r}, which does not define it"
        if self.defined_inside(name) and owner not in self.definitions[name]:
            return (
                f"{name!r} is defined in {sorted(self.definitions[name])} but patched on "
                f"{text!r}; an inside-defined seam must be patched on its defining module"
            )
        if not self.defined_inside(name) and owner not in self.consumers.get(name, set()):
            return (
                f"{name!r} comes from outside {self.package} and is patched on {text!r}, "
                f"which does not name-import it; patch the submodule that consumes it"
            )
        return None

    def assert_patch_targets(self) -> None:
        """Every patch lands on the module whose globals the consumer reads.

        Which module that is depends on where the name is defined, so the check
        branches on :meth:`defined_inside` rather than assuming one shape.
        """
        offenders = [
            offender
            for site in sorted(self.own_sites, key=lambda s: (s.attr, s.text))
            if (offender := self._target_offender(site.attr, self.owner(site) or FACADE, site.text))
        ]
        assert not offenders, "\n".join(dict.fromkeys(offenders))

    def assert_facade_withholds(self) -> None:
        """Re-exporting a seam lets a patch on the facade succeed and reach nobody."""
        for name in sorted(self.seams):
            assert not hasattr(self.module, name), (
                f"the facade re-exports the seam {name!r}; a patch on the facade would "
                f"silently reach nobody — patch the consuming submodule instead"
            )
            assert name not in getattr(self.module, "__all__", ())

    def _misbound(self, name: str, home: str) -> bool:
        """Is ``name`` a seam that module ``home`` has no business binding early?"""
        return name in self.seams and home not in self.patch_owners.get(name, set())

    def _routes_through(self, node: ast.ImportFrom, source: SourceFile) -> bool:
        module = absolute_module(node, source.home_package)
        return module == self.package or module.startswith(f"{self.package}.")

    def _import_offenders(self, source: SourceFile) -> list[str]:
        """``from X import seam`` in a module that is not the seam's patch target."""
        offenders: list[str] = []
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            # Outside the package only a route *through* it can launder a seam:
            # `from guidebot_recorder.scenario.compiled import write_compiled` in
            # render.py is render's own business and its own patch target.
            if not source.home and not self._routes_through(node, source):
                continue
            offenders += [
                f"{source.relative}:{node.lineno} imports the seam {alias.name!r} by name; "
                f"the patch lands on {sorted(self.patch_owners[alias.name])} and would not "
                f"reach this copy. Call it through the module that is patched"
                for alias in node.names
                if self._misbound(alias.name, source.home)
            ]
        return offenders

    def _seam_attribute(self, node: ast.expr | None) -> str | None:
        """The seam name in an ``<anything>.<seam>`` expression, else ``None``."""
        return node.attr if isinstance(node, ast.Attribute) and node.attr in self.seams else None

    def _alias_offenders(self, source: SourceFile) -> list[str]:
        """``alias = X.seam`` — an import-time snapshot with no import statement."""
        offenders: list[str] = []
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            seam = self._seam_attribute(node.value)
            if seam is not None and self._misbound(seam, source.home):
                offenders.append(
                    f"{source.relative}:{node.lineno} aliases the seam {seam!r} into a "
                    f"module-level name; the alias is bound at import time, so the patch on "
                    f"{sorted(self.patch_owners[seam])} would not reach it. Keep the "
                    f"attribute access at the call site"
                )
        return offenders

    def _default_offenders(self, source: SourceFile) -> list[str]:
        """``def f(..., _seam=X.seam)`` — a default is evaluated once, at def time."""
        offenders: list[str] = []
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.arguments):
                continue
            for default in [*node.defaults, *node.kw_defaults]:
                seam = self._seam_attribute(default)
                if seam is not None and self._misbound(seam, source.home):
                    offenders.append(
                        f"{source.relative}:{default.lineno} binds the seam {seam!r} as a "
                        f"parameter default; defaults are evaluated once at def time, so the "
                        f"patch on {sorted(self.patch_owners[seam])} would not reach it. Read "
                        f"the attribute inside the body"
                    )
        return offenders

    def assert_no_early_binding(self) -> None:
        """Three ways to snapshot a seam's *value* into the wrong module's globals."""
        offenders: list[str] = []
        for source in self.files:
            for find in (self._import_offenders, self._alias_offenders, self._default_offenders):
                offenders += find(source)
        assert not offenders, "\n".join(offenders)

    def coverage_offenders(self, name: str, consumers: set[str]) -> list[str]:
        """Test scopes that patch ``name`` on fewer than all of its ``consumers``."""
        patched: dict[tuple[str, str], set[str]] = defaultdict(set)
        for site in self.own_sites:
            if site.attr == name:
                patched[(site.where.rpartition(":")[0], site.scope)].add(self.owner(site) or FACADE)
        return [
            f"{where}::{scope} patches the seam {name!r} on {sorted(owners)} only; it is "
            f"name-imported by {sorted(consumers)}, so the copies in "
            f"{sorted(consumers - owners)} are never intercepted and that path disappears "
            f"without a trace. Patch every consuming submodule"
            for (where, scope), owners in sorted(patched.items())
            if consumers - owners
        ]

    def assert_multi_consumer_coverage(self) -> None:
        """A name-imported seam with two consumers needs two patch lines, not one.

        One line stops being enough the moment a second submodule pulls the name
        in: the second interception path silently disappears while the test still
        passes. Only outside-defined seams can have this shape — an inside-defined
        seam has exactly one home and may not be name-imported at all.
        """
        offenders: list[str] = []
        for name, consumers in sorted(self.consumers.items()):
            if len(consumers) > 1:
                offenders += self.coverage_offenders(name, consumers)
        assert not offenders, "\n".join(offenders)

    def assert_all(self) -> None:
        """Every check, for a package that wants one test instead of five."""
        self.assert_scan_complete()
        self.assert_patch_targets()
        self.assert_facade_withholds()
        self.assert_no_early_binding()
        self.assert_multi_consumer_coverage()
