"""Everything a render decides **before a browser exists**.

The first of the three lifetimes ``run_render`` used to interleave. A
:class:`_RenderPlan` is built once, is frozen, and is read by every later phase:
the scenario and its config, the compiled sidecar and the checks that say it may
be replayed at all, the desktop icons resolved up front, and the whole narration
pre-synthesized into the cache.

The ordering inside :func:`_prepare_render` is load-bearing and is why the
diagnostics half is its own small object. Every sidecar check must fail **before**
pre-synthesis, which spends minutes in a TTS provider; but those checks already
need the ``plik:linia`` banner, and the banner needs nothing the plan does not
already know. :class:`_Banner` is therefore built early, used by the validation,
and then handed to the finished plan — instead of building a half-empty plan or
threading four arguments through every check.

Icon resolution lives here for the same fail-loud reason it always did: an unknown
built-in or a missing file is an authoring error and must be reported before the
recording starts, not after minutes of render.

Nothing here is a test seam. ``write_compiled`` is one for ``recorder.compile``,
not for this package, and is name-imported exactly as ``_run`` used to import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from guidebot_recorder.desktop import resolve_icon
from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import COMPILER_VERSION, CachedAction, PendingAction
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import Config, TtsConfig, config_hash
from guidebot_recorder.models.scenario import FlatStep, Scenario
from guidebot_recorder.recorder._debug import scenario_sensitive_values
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.scenario.source import ScenarioSource
from guidebot_recorder.tts.base import Segment, TtsCache, TtsProvider

from .errors import RenderError
from .narration import _narration, _presynthesize_narration
from .reuse import _compiled_action_is_current


@dataclass(frozen=True, slots=True)
class _Banner:
    """The `plik:linia` + YAML-fragment banner every render message is wrapped in.

    Split out of :class:`_RenderPlan` because the sidecar validation needs it
    before the plan can exist — see the module docstring. It carries exactly the
    three things :func:`~guidebot_recorder.diagnostics.step_banner` cannot derive
    from a step: which file, how many steps there are, and which strings must be
    redacted.
    """

    source: ScenarioSource | None
    total: int
    sensitive: tuple[str, ...]

    def message(self, entry: FlatStep, index: int, message: str, *, warning: bool = False) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=index,
            total=self.total,
            location=entry.location,
            source=self.source,
            message=message,
            warning=warning,
            sensitive=self.sensitive,
        )

    def note_skip(self, entry: FlatStep, index: int, reason: str, *, gate: bool) -> None:
        """Odnotuj pominięty krok opcjonalny — banner z `plik:linia`."""

        what = "bramka" if gate else "krok opcjonalny"
        tqdm.write(self.message(entry, index, f"{what} pominięty — {reason}", warning=True))


@dataclass(frozen=True, slots=True)
class _RenderPlan:
    """What the render will replay, decided before the browser opens.

    Frozen: nothing observed during the recording belongs here. ``compiled`` is
    the one exception and it is deliberate — :meth:`persist_resolved` folds a
    render-time resolution of a *pending* entry back into the sidecar, which is
    an edit to the plan's own on-disk source, not recorded state.
    """

    path: Path
    out_mp4: Path
    work: Path
    scenario: Scenario
    cfg: Config
    banner: _Banner
    sensitive_values: tuple[str, ...]
    flat: list[FlatStep]
    compiled: CompiledScenario
    sidecar: Path
    audio_configs: list[TtsConfig]
    scenario_hash: str
    desktop_payloads: dict[int, dict[str, str]]
    segments: dict[str, dict[int, Segment]]
    verbose: bool

    @property
    def total(self) -> int:
        return len(self.flat)

    def step_message(
        self, entry: FlatStep, index: int, message: str, *, warning: bool = False
    ) -> str:
        return self.banner.message(entry, index, message, warning=warning)

    def note_skip(self, entry: FlatStep, index: int, reason: str, *, gate: bool) -> None:
        self.banner.note_skip(entry, index, reason, gate=gate)

    def persist_resolved(self, index: int, resolved_action: CachedAction) -> None:
        """Fold a render-time resolution back into the sidecar (full atomic rewrite)."""

        self.compiled.actions[index] = resolved_action
        write_compiled(self.sidecar, self.compiled)


def _apply_overrides(cfg: Config, hold_frame: bool | None, hold_frame_settle: float | None) -> None:
    """Caller-side overrides (the CLI flags).

    ``None`` means "use whatever the scenario configured" — the scenario is loaded
    here, so an override applied to a Config built by the caller would be discarded.
    """

    if hold_frame is not None:
        cfg.hold_frame_for_narration = hold_frame
    if hold_frame_settle is not None:
        cfg.hold_frame_settle = hold_frame_settle


def _assert_one_provider(audio_configs: list[TtsConfig]) -> None:
    providers = {tts.provider for tts in audio_configs}
    if len(providers) != 1:
        raise RenderError(
            "jeden render obsługuje obecnie jeden provider TTS; "
            f"skonfigurowano: {', '.join(sorted(providers))}"
        )


def _load_sidecar(path: Path, flat: list[FlatStep]) -> tuple[Path, CompiledScenario]:
    """Read ``*.compiled.yaml`` and reject every shape render cannot replay."""

    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise RenderError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if compiled.source != path.name:
        raise RenderError(
            f"compiled pochodzi z innego scenariusza ({compiled.source}) — uruchom `compile`"
        )
    if len(compiled.actions) != len(flat):
        raise RenderError("compiled niezgodny z liczbą kroków — uruchom `compile`")
    if compiled.compiler_version != COMPILER_VERSION or any(
        action is not None and action.fingerprint.compiler_version != COMPILER_VERSION
        for action in compiled.actions
    ):
        raise RenderError("compiled ma starszą wersję — uruchom `compile`")
    return cpath, compiled


def _assert_entries_current(
    flat: list[FlatStep], compiled: CompiledScenario, scenario_hash: str, banner: _Banner
) -> None:
    """Per-entry sidecar checks, in banner form (`plik:linia` + YAML fragment)."""

    for index, (entry, action) in enumerate(zip(flat, compiled.actions, strict=True)):
        if not _compiled_action_is_current(entry.step, action, scenario_hash):
            raise RenderError(
                banner.message(entry, index, "compiled jest nieaktualny — uruchom `compile`")
            )
        if isinstance(action, PendingAction) and entry.branch is None and not entry.step.optional:
            # A pending entry is only ever written for a branch (gate + children)
            # or an `optional: true` step; anywhere else the sidecar is corrupt.
            raise RenderError(
                banner.message(
                    entry, index, "wpis oczekujący na kroku obowiązkowym — uruchom `compile`"
                )
            )


def _resolve_desktop_payloads(
    cfg: Config, flat: list[FlatStep], base_dir: Path
) -> dict[int, dict[str, str]]:
    """Desktop icons, resolved before recording so an authoring error fails up front.

    An unknown built-in or a missing file must fail loud here, not after minutes
    of render. Relative icon paths resolve against the scenario file's directory.
    Keyed by flat-step index for the render loop to read back.
    """

    payloads: dict[int, dict[str, str]] = {}
    for index, entry in enumerate(flat):
        if entry.step.desktop is not None:
            payloads[index] = {
                "color": cfg.desktop.color,
                "label": entry.step.desktop.label,
                **resolve_icon(entry.step.desktop, base_dir=base_dir),
            }
    return payloads


async def _presynthesize(
    flat: list[FlatStep],
    audio_configs: list[TtsConfig],
    cache_dir: Path | str,
    tts_provider: TtsProvider,
    *,
    verbose: bool,
) -> dict[str, dict[int, Segment]]:
    """Faza 0: pre-synteza całej narracji (fail-loud przed nagrywaniem)."""

    steps = [entry.step for entry in flat]
    cache = TtsCache(cache_dir)
    narration_count = sum(_narration(step) is not None for step in steps)
    presynth = tqdm(
        total=narration_count * len(audio_configs),
        desc="tts",
        unit="segment",
        disable=not verbose,
    )
    try:
        return await _presynthesize_narration(
            steps,
            audio_configs,
            cache,
            tts_provider,
            on_progress=presynth.update,
        )
    finally:
        presynth.close()


async def _prepare_render(
    path: Path | str,
    out_mp4: Path | str,
    tts_provider: TtsProvider,
    cache_dir: Path | str,
    *,
    env: Mapping[str, str] | None,
    hold_frame: bool | None,
    hold_frame_settle: float | None,
    verbose: bool,
) -> _RenderPlan:
    """Validate everything replayable, resolve the icons, synthesize the voice-over.

    The order is the contract: every sidecar rejection happens before
    :func:`_presynthesize` is allowed to spend a minute in a TTS provider.
    """

    path = Path(path)
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    scenario = load_scenario(path, env)
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cfg = scenario.config
    _apply_overrides(cfg, hold_frame, hold_frame_settle)
    audio_configs = [cfg.tts, *cfg.audio_tracks]
    _assert_one_provider(audio_configs)

    # Flat indexing: a `when:` block contributes its synthetic gate step followed by
    # its children, so `actions`, narration segments and every `krok {index}` message
    # index the same linear execution order.
    flat = scenario.flat_steps()
    sidecar, compiled = _load_sidecar(path, flat)
    scenario_hash = config_hash(cfg)
    banner = _Banner(source=scenario.source, total=len(flat), sensitive=sensitive_values)
    _assert_entries_current(flat, compiled, scenario_hash, banner)

    return _RenderPlan(
        path=path,
        out_mp4=out_mp4,
        # The recording/staging directory: everything ffmpeg touches on the way to
        # the master lives here, beside the output rather than in a temp root.
        work=out_mp4.parent / ".guidebot_video" / out_mp4.stem,
        scenario=scenario,
        cfg=cfg,
        banner=banner,
        sensitive_values=sensitive_values,
        flat=flat,
        compiled=compiled,
        sidecar=sidecar,
        audio_configs=audio_configs,
        scenario_hash=scenario_hash,
        desktop_payloads=_resolve_desktop_payloads(cfg, flat, path.parent),
        segments=await _presynthesize(
            flat, audio_configs, cache_dir, tts_provider, verbose=verbose
        ),
        verbose=verbose,
    )
