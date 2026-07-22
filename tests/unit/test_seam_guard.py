"""Tests for the seam-guard machinery itself — the parts nothing else exercises yet.

``tests/unit/video/test_mux_seams.py`` and ``tests/unit/recorder/test_compile_seams.py``
drive :mod:`tests._seam_scan` and :mod:`tests._seam_guard` against two real
packages, and between them they
cover one discovery idiom (``importlib.import_module``), one patch shape
(``<alias>.<submodule>``) and one consumer per seam. ``recorder.render`` — phase 1c
— has none of those properties: it is patched through three *other* idioms, most of
its seams are defined outside the package, and two of them will have two consumers
after the split. A guard whose new capabilities are never exercised is a guard that
discovers nothing on the day it matters, so they are exercised here, on synthetic
input and on the real ``render`` surface.

Needs nothing but source: no ffmpeg, no browser, no network.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

from tests._seam_guard import SeamGuard
from tests._seam_scan import (
    ALIAS_FORM,
    DOTTED_FORM,
    UNKNOWN_FORM,
    PatchSite,
    SourceFile,
    module_aliases,
    patch_sites,
)

RENDER = "guidebot_recorder.recorder.render"


def _sites(source: str, home_package: str = "tests.unit") -> list[PatchSite]:
    tree = ast.parse(source)
    return patch_sites(tree, module_aliases(tree, home_package), "snippet.py")


# --------------------------------------------------------------------------- #
# discovery: the four binding idioms
# --------------------------------------------------------------------------- #


def test_import_as_binds_a_module_alias() -> None:
    # 26 of render's patch sites use this form and the old scan saw none of them:
    # it only ever looked for importlib.import_module.
    (site,) = _sites(
        "import guidebot_recorder.recorder.render as R\n"
        'def test_x(monkeypatch):\n    monkeypatch.setattr(R, "_pace_narration", None)\n'
    )
    assert (site.form, site.path, site.attr) == (ALIAS_FORM, RENDER, "_pace_narration")


def test_from_import_as_binds_a_module_alias() -> None:
    (site,) = _sites(
        "from guidebot_recorder.recorder import render as render_module\n"
        'def test_x(monkeypatch):\n    monkeypatch.setattr(render_module, "Recorder", None)\n'
    )
    assert (site.form, site.path, site.attr) == (ALIAS_FORM, RENDER, "Recorder")


def test_importlib_import_module_binds_a_module_alias() -> None:
    (site,) = _sites(
        "import importlib\n"
        'm = importlib.import_module("guidebot_recorder.video.mux")\n'
        'def test_x(monkeypatch):\n    monkeypatch.setattr(m.ffmpeg, "_run", None)\n'
    )
    assert site.path == "guidebot_recorder.video.mux.ffmpeg"
    assert (site.form, site.attr) == (ALIAS_FORM, "_run")


def test_dotted_string_target_needs_no_alias_at_all() -> None:
    # pytest's two-argument form: the module path and the attribute are one string,
    # so the last component is the name being replaced.
    (site,) = _sites(
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr("guidebot_recorder.recorder.render.build_audio_bed", None)\n'
    )
    assert (site.form, site.path, site.attr) == (DOTTED_FORM, RENDER, "build_audio_bed")


def test_a_target_the_scan_cannot_attribute_is_marked_unknown() -> None:
    # Not an error by itself — `monkeypatch.setattr(page, "goto", ...)` patches a
    # runtime object and is nobody's seam. It becomes an error only once the
    # evidence mentions the guarded package; see below.
    (site,) = _sites('def test_x(monkeypatch, page):\n    monkeypatch.setattr(page, "goto", None)\n')
    assert (site.form, site.path) == (UNKNOWN_FORM, None)


def test_evidence_carries_the_binding_statement_not_only_the_target_text() -> None:
    # `rm` mentions nothing. Its assignment launders a module through a form no
    # alias rule recognises, and that is exactly the case a completeness check
    # must not wave through, so the binding text joins the evidence.
    (site,) = _sites(
        "import sys\n"
        'rm = sys.modules["guidebot_recorder.recorder.render"]\n'
        'def test_x(monkeypatch):\n    monkeypatch.setattr(rm, "_render_step", None)\n'
    )
    assert site.form == UNKNOWN_FORM
    assert RENDER in site.evidence


# --------------------------------------------------------------------------- #
# (f) the render simulation — the capability phase 1c inherits
# --------------------------------------------------------------------------- #

RENDER_GUARD = SeamGuard.build(RENDER)


def test_the_render_surface_is_discovered_before_the_split() -> None:
    # `render.py` is still one module, so `package_dir` is the directory the split
    # will create and holds no files yet. Discovery does not care: it works off the
    # dotted name, which is why this measurement is available *before* 1c starts.
    assert not RENDER_GUARD.package_dir.exists()

    # The old importlib-only scan found 0 of these. Floors, not exact counts —
    # 1c will move sites around and the point is that they stay visible.
    sites = RENDER_GUARD.own_sites
    assert len(sites) >= 60, f"only {len(sites)} render patch sites discovered"
    assert len(RENDER_GUARD.seams) >= 19, sorted(RENDER_GUARD.seams)

    # Both idioms render actually uses must be represented; a regression that
    # silently dropped one would still clear the floors above.
    forms = {site.form for site in sites}
    assert forms == {ALIAS_FORM, DOTTED_FORM}, forms


def test_no_render_patch_site_anywhere_is_unclassified() -> None:
    # The real deliverable for 1c: the list below must stay empty. Anything in it
    # is a patch the guard cannot see, and therefore a seam 1c could break in
    # silence. `completeness_offenders` is what `assert_scan_complete` raises on.
    assert RENDER_GUARD.completeness_offenders() == []


def test_render_is_dominated_by_the_mode_mux_does_not_implement() -> None:
    # 10 of the patched names are not defined in render.py at all — they are
    # cross-package name-imports (`Overlay`, `Recorder`, `probe_frame_count`, ...).
    # A mux-shaped guard would call every one of their patch sites a violation.
    imported = {
        name
        for name in RENDER_GUARD.seams
        if name in _name_imports(RENDER_GUARD.source_root / "recorder" / "render.py")
    }
    assert len(imported) >= 10, sorted(imported)


def _name_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


# --------------------------------------------------------------------------- #
# (g) the multi-consumer rule, on a package built for the purpose
# --------------------------------------------------------------------------- #


def _synthetic(consumer_sources: dict[str, str], patched_on: list[str]) -> SeamGuard:
    """A guard over an imaginary package, so rule 3 can be provoked on demand.

    No package in the repo has a two-consumer seam yet — that shape arrives with
    the render split — and a rule that has never fired is a rule nobody has
    checked. Building the dataclass directly keeps the check honest without
    waiting for 1c or writing a throwaway package to disk.
    """
    files = tuple(
        SourceFile(
            path=Path(f"pkg/{home}.py"),
            relative=f"pkg/{home}.py",
            tree=ast.parse(source),
            home=home,
            home_package="pkg",
        )
        for home, source in consumer_sources.items()
    )
    sites = tuple(
        PatchSite(
            where=f"tests/test_pkg.py:{10 + n}",
            scope="test_one_thing",
            text=f"pkg_module.{owner}",
            form=ALIAS_FORM,
            path=f"pkg.{owner}",
            attr="Recorder",
            evidence=f"pkg_module.{owner}",
        )
        for n, owner in enumerate(patched_on)
    )
    return SeamGuard(
        package="pkg",
        module=types.SimpleNamespace(),  # type: ignore[arg-type]
        package_dir=Path("pkg"),
        source_root=Path("."),
        tests_root=Path("tests"),
        sites=sites,
        files=files,
    )


TWO_CONSUMERS = {
    "run": "from guidebot_recorder.recorder.recorder import Recorder\n",
    "popup": "from guidebot_recorder.recorder.recorder import Recorder\n",
}


def test_one_patch_line_is_not_enough_for_a_two_consumer_seam() -> None:
    # The spec names `Recorder` and `probe_frame_count` as exactly this shape in
    # render: two submodules name-import the same outside name, so each holds its
    # own early-bound copy. Patching one leaves the other live, and the test that
    # thought it had intercepted the call keeps passing.
    guard = _synthetic(TWO_CONSUMERS, patched_on=["run"])
    assert guard.consumers == {"Recorder": {"popup", "run"}}
    offenders = guard.coverage_offenders("Recorder", {"popup", "run"})
    assert offenders and "popup" in offenders[0]

    try:
        guard.assert_multi_consumer_coverage()
    except AssertionError as exc:
        assert "'Recorder'" in str(exc)
    else:  # pragma: no cover - the whole point of the test
        raise AssertionError("the multi-consumer rule did not fire")


def test_patching_every_consumer_satisfies_the_rule() -> None:
    guard = _synthetic(TWO_CONSUMERS, patched_on=["run", "popup"])
    guard.assert_multi_consumer_coverage()


def test_a_single_consumer_seam_needs_only_one_patch_line() -> None:
    # Today's shape for both compile seams. The rule must stay quiet here, or it
    # would fire on every correctly-patched package in the repo.
    guard = _synthetic(
        {"run": TWO_CONSUMERS["run"], "popup": "\n"},
        patched_on=["run"],
    )
    guard.assert_multi_consumer_coverage()
