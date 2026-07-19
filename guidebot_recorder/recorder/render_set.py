"""Compile and render a set of complete, locale-specific scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser

from guidebot_recorder.recorder._debug import redact_exception, scenario_sensitive_values
from guidebot_recorder.recorder.compile import compile_up_to_date, run_compile_in_browser
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.scenario.render_set import RenderSetPlan
from guidebot_recorder.tts.base import TtsProvider


class RenderSetError(RuntimeError):
    """A localized set cannot be compiled or rendered safely."""


@dataclass(frozen=True, slots=True)
class CompileSetResult:
    """Language ids compiled now versus safely reused from their sidecars."""

    compiled: tuple[str, ...]
    reused: tuple[str, ...]


def _safe_variant_error(
    scenario_path: Path,
    env: Mapping[str, str] | None,
    exc: Exception,
) -> str:
    """Keep explicit input values out of set-level CLI errors."""

    try:
        sensitive_values = scenario_sensitive_values(
            load_scenario(scenario_path, env), scenario_env_references(scenario_path, env)
        )
    except Exception:
        return type(exc).__name__
    return redact_exception(exc, sensitive_values)


def _stale_languages(
    plan: RenderSetPlan,
    env: Mapping[str, str] | None,
) -> list[str]:
    stale: list[str] = []
    for variant in plan.variants:
        try:
            current = compile_up_to_date(variant.scenario, env)
        except Exception as exc:
            detail = _safe_variant_error(variant.scenario, env, exc)
            raise RenderSetError(
                f"nie można sprawdzić compiled wariantu {variant.language}: {detail}"
            ) from None
        if not current:
            stale.append(variant.language)
    return stale


def render_set_up_to_date(
    plan: RenderSetPlan,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether every variant has a current, source-matched sidecar."""

    return not _stale_languages(plan, env)


def _path_parts(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _contains(parent: Path, child: Path) -> bool:
    parent_parts = _path_parts(parent)
    child_parts = _path_parts(child)
    return child_parts[: len(parent_parts)] == parent_parts


def _overlaps(first: Path, second: Path) -> bool:
    return _contains(first, second) or _contains(second, first)


def render_set_output_paths(
    plan: RenderSetPlan,
    out_dir: Path | str,
) -> tuple[Path, ...]:
    """Resolve and preflight final MP4s plus their private render workspaces."""

    root = Path(out_dir).resolve()
    outputs: list[Path] = []
    workspaces: list[Path] = []
    for variant in plan.variants:
        requested = root / variant.output
        parent = requested.parent.resolve()
        try:
            parent.relative_to(root)
        except ValueError:
            raise RenderSetError(
                f"output wariantu {variant.language} wychodzi poza --output-dir po rozwiązaniu "
                "linków symbolicznych"
            ) from None
        output = parent / requested.name
        workspace = (parent / ".guidebot_video" / output.stem).resolve()
        try:
            workspace.relative_to(root)
        except ValueError:
            raise RenderSetError(
                f"katalog roboczy wariantu {variant.language} wychodzi poza --output-dir"
            ) from None
        outputs.append(output)
        workspaces.append(workspace)

    for index, output in enumerate(outputs):
        for other in outputs[index + 1 :]:
            if _overlaps(output, other):
                raise RenderSetError(
                    "docelowe MP4 wariantów kolidują po rozwiązaniu ścieżek lub linków "
                    "symbolicznych"
                )
    for index, workspace in enumerate(workspaces):
        for other in workspaces[index + 1 :]:
            if _overlaps(workspace, other):
                raise RenderSetError("katalogi robocze wariantów nakładają się")
        for output in outputs:
            if _overlaps(workspace, output):
                raise RenderSetError(
                    "docelowy MP4 wariantu koliduje z katalogiem roboczym innego wariantu"
                )
    return tuple(outputs)


def ensure_render_set_compiled(
    plan: RenderSetPlan,
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail before TTS/browser use unless every variant has a current sidecar."""

    stale = _stale_languages(plan, env)
    if stale:
        raise RenderSetError(
            "brak aktualnego compiled dla wariantów: "
            f"{', '.join(stale)} — uruchom `guidebot compile-set`"
        )


async def run_compile_set(
    plan: RenderSetPlan,
    browser: Browser,
    reasoner: Reasoner,
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 30.0,
    force: bool = False,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> CompileSetResult:
    """Compile variants in manifest order, each in a fresh locale-aware context."""

    compiled: list[str] = []
    reused: list[str] = []
    for variant in plan.variants:
        if not force:
            try:
                current = compile_up_to_date(variant.scenario, env)
            except Exception as exc:
                detail = _safe_variant_error(variant.scenario, env, exc)
                raise RenderSetError(f"compile wariantu {variant.language}: {detail}") from None
            if current:
                reused.append(variant.language)
                continue
        try:
            await run_compile_in_browser(
                variant.scenario,
                browser,
                reasoner,
                env,
                timeout=timeout,
                force=force,
                pause_on_error=pause_on_error,
                verbose=verbose,
            )
        except Exception as exc:
            detail = _safe_variant_error(variant.scenario, env, exc)
            raise RenderSetError(f"compile wariantu {variant.language}: {detail}") from None
        compiled.append(variant.language)
    return CompileSetResult(compiled=tuple(compiled), reused=tuple(reused))


async def run_render_set(
    plan: RenderSetPlan,
    out_dir: Path | str,
    tts_provider: TtsProvider,
    cache_dir: Path | str,
    browser: Browser,
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 30.0,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> list[Path]:
    """Render one single-audio MP4 per variant in deterministic manifest order.

    The full compiled preflight happens before the browser is touched. If a later
    synthesis or render fails, processing stops; outputs completed earlier remain
    valid and atomically published by :func:`run_render`.
    """

    outputs = render_set_output_paths(plan, out_dir)
    ensure_render_set_compiled(plan, env)
    rendered: list[Path] = []
    for variant, output in zip(plan.variants, outputs, strict=True):
        try:
            await run_render(
                variant.scenario,
                output,
                tts_provider,
                cache_dir,
                browser,
                env=env,
                timeout=timeout,
                pause_on_error=pause_on_error,
                verbose=verbose,
            )
        except Exception as exc:
            detail = _safe_variant_error(variant.scenario, env, exc)
            raise RenderSetError(f"render wariantu {variant.language}: {detail}") from None
        rendered.append(output)
    return rendered
