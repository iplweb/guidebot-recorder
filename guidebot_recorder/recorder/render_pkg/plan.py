"""Wszystko, co da się rozstrzygnąć ZANIM otworzy się przeglądarka.

Prolog renderu jest w całości fail-loud: niezgodny sidecar, nieznana ikona
pulpitu czy dwa różne providery TTS mają wywalić się natychmiast, a nie po
kilku minutach nagrywania. Zebranie tego w osobny moduł sprawia, że da się to
przetestować bez przeglądarki — i że ``run_render`` zaczyna się od jednej
linijki zamiast od stu.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from guidebot_recorder.desktop import resolve_icon
from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import COMPILER_VERSION, PendingAction
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import Config, TtsConfig, config_hash
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step
from guidebot_recorder.recorder._debug import scenario_sensitive_values
from guidebot_recorder.recorder.render_pkg.actions import _compiled_action_is_current
from guidebot_recorder.recorder.render_pkg.errors import RenderError
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references


@dataclass(slots=True)
class RenderPlan:
    """Zwalidowany scenariusz wraz z sidecarem, gotowy do nagrywania."""

    path: Path
    scenario: Scenario
    cfg: Config
    flat: list[FlatStep]
    compiled: CompiledScenario
    cpath: Path
    scenario_hash: str
    sensitive_values: frozenset[str] | set[str] | tuple[str, ...]
    audio_configs: list[TtsConfig]
    #: payload ikony pulpitu, kluczowany indeksem płaskiego kroku
    desktop_payloads: dict[int, dict[str, str]]

    @property
    def flat_steps(self) -> list[Step]:
        return [entry.step for entry in self.flat]

    @property
    def total(self) -> int:
        return len(self.flat)

    def step_message(self, entry: FlatStep, index: int, message: str, *, warning: bool = False):
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=index,
            total=self.total,
            location=entry.location,
            source=self.scenario.source,
            message=message,
            warning=warning,
            sensitive=self.sensitive_values,
        )


def _single_tts_provider(audio_configs: list[TtsConfig]) -> None:
    """Jeden render = jeden provider TTS (ograniczenie v1)."""

    providers = {tts.provider for tts in audio_configs}
    if len(providers) != 1:
        raise RenderError(
            "jeden render obsługuje obecnie jeden provider TTS; "
            f"skonfigurowano: {', '.join(sorted(providers))}"
        )


def _load_sidecar(path: Path, n_steps: int) -> tuple[CompiledScenario, Path]:
    """Wczytaj `*.compiled.yaml` i odrzuć każdą niezgodność z bieżącym scenariuszem."""

    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise RenderError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if compiled.source != path.name:
        raise RenderError(
            f"compiled pochodzi z innego scenariusza ({compiled.source}) — uruchom `compile`"
        )
    if len(compiled.actions) != n_steps:
        raise RenderError("compiled niezgodny z liczbą kroków — uruchom `compile`")
    if compiled.compiler_version != COMPILER_VERSION or any(
        action is not None and action.fingerprint.compiler_version != COMPILER_VERSION
        for action in compiled.actions
    ):
        raise RenderError("compiled ma starszą wersję — uruchom `compile`")
    return compiled, cpath


def _assert_actions_current(plan: RenderPlan) -> None:
    """Każda akcja musi odpowiadać swojemu krokowi i bieżącej konfiguracji."""

    for index, (entry, action) in enumerate(
        zip(plan.flat, plan.compiled.actions, strict=True)
    ):
        if not _compiled_action_is_current(entry.step, action, plan.scenario_hash):
            raise RenderError(
                plan.step_message(entry, index, "compiled jest nieaktualny — uruchom `compile`")
            )
        if isinstance(action, PendingAction) and entry.branch is None and not entry.step.optional:
            # A pending entry is only ever written for a branch (gate + children)
            # or an `optional: true` step; anywhere else the sidecar is corrupt.
            raise RenderError(
                plan.step_message(
                    entry, index, "wpis oczekujący na kroku obowiązkowym — uruchom `compile`"
                )
            )


def _resolve_desktop_icons(flat_steps: list[Step], cfg: Config, base_dir: Path):
    """Rozwiąż ikony pulpitu przed nagrywaniem.

    Nieznany wbudowany identyfikator albo brakujący plik to błąd autora i musi
    wywalić się od razu, a nie po minutach renderu. Ścieżki względne rozwiązują
    się względem katalogu scenariusza.
    """

    payloads: dict[int, dict[str, str]] = {}
    for index, step in enumerate(flat_steps):
        if step.desktop is not None:
            payloads[index] = {
                "color": cfg.desktop.color,
                "label": step.desktop.label,
                **resolve_icon(step.desktop, base_dir=base_dir),
            }
    return payloads


def load_render_plan(
    path: Path,
    env: Mapping[str, str] | None,
    *,
    hold_frame: bool | None,
    hold_frame_settle: float | None,
) -> RenderPlan:
    """Wczytaj i zwaliduj wszystko, co render musi wiedzieć przed nagrywaniem."""

    scenario = load_scenario(path, env)
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cfg = scenario.config
    # Caller-side overrides (the CLI flags). ``None`` means "use whatever the
    # scenario configured" — the scenario is loaded here, so an override applied
    # to a Config built by the caller would be discarded.
    if hold_frame is not None:
        cfg.hold_frame_for_narration = hold_frame
    if hold_frame_settle is not None:
        cfg.hold_frame_settle = hold_frame_settle
    audio_configs = [cfg.tts, *cfg.audio_tracks]
    _single_tts_provider(audio_configs)

    # Flat indexing: a `when:` block contributes its synthetic gate step followed by
    # its children, so `actions`, narration segments and every `krok {index}` message
    # index the same linear execution order.
    flat = scenario.flat_steps()
    compiled, cpath = _load_sidecar(path, len(flat))

    plan = RenderPlan(
        path=path,
        scenario=scenario,
        cfg=cfg,
        flat=flat,
        compiled=compiled,
        cpath=cpath,
        scenario_hash=config_hash(cfg),
        sensitive_values=sensitive_values,
        audio_configs=audio_configs,
        desktop_payloads={},
    )
    _assert_actions_current(plan)
    plan.desktop_payloads = _resolve_desktop_icons(plan.flat_steps, cfg, path.parent)
    return plan
