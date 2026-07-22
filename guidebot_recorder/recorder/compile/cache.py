"""The sidecar reuse contract: what may be reused, and what forces a recompile.

One question is answered here in two halves that must not be merged. The
artifact-level half (:func:`_compiled_artifact_is_current`) asks whether the
``*.compiled.yaml`` as a whole still answers the source; the per-step half
(:func:`_can_reuse`, over :func:`_fingerprint_matches`) asks the same of a single
frozen action. :func:`compile_up_to_date` composes them into the answer the CLI
uses to skip Chromium entirely, and :func:`needs_positional_recheck` is the
deliberately separate question that a browser — and only a browser — can answer.

No Playwright import: every function here reads files and pydantic models, which
is what lets the CLI ask them before deciding to launch a browser at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from guidebot_recorder.models.action import (
    COMPILER_VERSION,
    CachedAction,
    Fingerprint,
    PendingAction,
)
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.scenario import FlatStep, Step
from guidebot_recorder.models.target import RoleTarget, Target
from guidebot_recorder.resolver.resolution import compiled_from, step_state
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario


def _carries_positional_index(action: CompiledAction | None) -> bool:
    """Czy zamrożona akcja niesie indeks pozycyjny — także w zagnieżdżonym `scope`."""

    if not isinstance(action, CachedAction):
        return False
    target: Target | None = action.target
    while target is not None:
        if isinstance(target, RoleTarget) and target.nth is not None:
            return True
        target = target.scope
    return False


def _load_prior_actions(cpath: Path, n_steps: int) -> list[CompiledAction | None]:
    """Load existing compiled actions for reuse, aligned by index to the current steps.

    Steps appended at the end stay ``None`` (to be resolved). If a step is inserted
    or removed mid-scenario the indices shift and the per-step fingerprint check
    (:func:`_can_reuse`) will simply re-resolve the affected steps — correctness is
    never traded for the incremental speed-up.
    """
    result: list[CompiledAction | None] = [None] * n_steps
    if not cpath.exists():
        return result
    try:
        prior = load_compiled(cpath)
    except Exception:  # noqa: BLE001 — a corrupt/stale compiled file just means recompile
        return result
    if prior.compiler_version != COMPILER_VERSION:
        return result
    for i in range(min(len(prior.actions), n_steps)):
        result[i] = prior.actions[i]
    return result


def _steps_needing_resolution(
    flat: list[FlatStep], actions: list[CompiledAction | None], chash: str, force: bool
) -> list[int]:
    """Flat indices of target steps whose frozen action is missing or stale."""
    return [
        i
        for i, entry in enumerate(flat)
        if entry.step.requires_target() and not _can_reuse(actions[i], entry.step, chash, force)
    ]


def compile_up_to_date(
    path: Path | str, env: Mapping[str, str] | None = None, *, force: bool = False
) -> bool:
    """True if every target step already has a valid frozen action — no browser needed.

    Lets the CLI skip launching Chromium entirely when the only edits were to
    non-target steps (e.g. ``say`` narration) or nothing at all.

    One question only: **does the sidecar answer the source?** Whether a frozen
    positional index still points where it did is a different question with a
    different answer, and it lives in :func:`needs_positional_recheck`. Folding
    it in here would make a freshly built sidecar report itself stale, and the
    render-set preflight (which asks precisely this question) would demand a
    ``compile-set`` that had just succeeded.
    """
    if force:
        return False
    path = Path(path)
    scenario = load_scenario(path, env)
    chash = config_hash(scenario.config)
    cpath = compiled_path(path)
    flat = scenario.flat_steps()
    if not _compiled_artifact_is_current(cpath, path.name, len(flat)):
        return False
    actions = _load_prior_actions(cpath, len(flat))
    if any(
        not entry.step.requires_target() and actions[index] is not None
        for index, entry in enumerate(flat)
    ):
        return False
    return not _steps_needing_resolution(flat, actions, chash, force)


def needs_positional_recheck(path: Path | str, env: Mapping[str, str] | None = None) -> bool:
    """Czy trzeba otworzyć przeglądarkę, bo zamrożony jest namiar pozycyjny?

    Osobne pytanie od :func:`compile_up_to_date`, i celowo osobna funkcja.
    Tamta odpowiada „czy sidecar odpowiada źródłu" — pytanie, na które
    ``render``/``render-set`` opierają swój preflight, i na które świeżo
    zbudowany sidecar zawsze odpowiada „tak". To pytanie brzmi inaczej: odcisk
    kroku (wersja kompilatora, rodzaj polecenia, tekst źródła, hash configu,
    stan) nie mówi nic o stronie, więc przebudowany DOM zostawia go
    identycznym. Zamrożony ``nth`` jest wart tyle, co strona, na której go
    zmierzono — a jedynym miejscem, gdzie dryf da się sprawdzić, jest otwarta
    przeglądarka.

    Koszt jest zamierzony i wąski: scenariusz z namiarem pozycyjnym uruchamia
    Chromium przy każdej kompilacji. To dokładnie ten scenariusz, który po
    cichu gnije. Wpięte **wyłącznie** w bramki kompilacji (``compile``,
    ``compile-set``); preflight renderu pyta o co innego i pytać o to nie może,
    bo świeży sidecar z ``nth`` unieruchomiłby ``render-set`` na zawsze.
    """

    path = Path(path)
    scenario = load_scenario(path, env)
    flat = scenario.flat_steps()
    actions = _load_prior_actions(compiled_path(path), len(flat))
    return any(_carries_positional_index(action) for action in actions)


def _compiled_artifact_is_current(cpath: Path, source_name: str, n_steps: int) -> bool:
    """Validate artifact-level invariants, including targetless scenarios."""

    try:
        compiled = load_compiled(cpath)
    except Exception:  # noqa: BLE001 — missing/corrupt artifacts simply need compile
        return False
    return (
        compiled.compiler_version == COMPILER_VERSION
        and compiled.source == source_name
        and len(compiled.actions) == n_steps
        and all(
            action is None or action.fingerprint.compiler_version == COMPILER_VERSION
            for action in compiled.actions
        )
    )


def _pending_for(step: Step, chash: str) -> PendingAction:
    """Placeholder for a target that was optional and absent at compile time.

    ``expect`` is only settled by actually performing the action, so the
    fingerprint carries the neutral ``"none"``; the entry exists to keep the
    usual version/config invalidation working until render resolves it.
    """

    return PendingAction(
        fingerprint=Fingerprint(
            command_kind=step.command_kind(),
            compiled_from=compiled_from(step),
            expect="none",
            config_hash=chash,
            state=step_state(step),
        )
    )


def _fingerprint_matches(fp: Fingerprint, step: Step, chash: str) -> bool:
    return (
        fp.compiler_version == COMPILER_VERSION
        and fp.command_kind == step.command_kind()
        and fp.compiled_from == compiled_from(step)
        and fp.config_hash == chash
        and fp.state == step_state(step)
    )


def _can_reuse(cached_in: CompiledAction | None, step: Step, chash: str, force: bool) -> bool:
    """Reuse only if the frozen fingerprint still matches the source and config.

    A :class:`PendingAction` counts as reusable on purpose: the element it stands
    for is optional, so retrying it would launch a browser and burn the full gate
    timeout on every compile for something that may never be there. ``--force``
    re-attempts. It has no ``expect`` to cross-check — that is only settled once
    the action actually runs.
    """
    if force or cached_in is None:
        return False
    if isinstance(cached_in, PendingAction):
        return _fingerprint_matches(cached_in.fingerprint, step, chash)
    return (
        _fingerprint_matches(cached_in.fingerprint, step, chash)
        and cached_in.fingerprint.expect == cached_in.expect
    )
