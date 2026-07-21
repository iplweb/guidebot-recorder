"""Load and preflight a localized render-set manifest without exposing secrets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ruamel.yaml import YAML

from guidebot_recorder.models.render_set import LocalizedRenderSet
from guidebot_recorder.scenario.compiled import compiled_path
from guidebot_recorder.scenario.loader import ScenarioValidationError, load_scenario

_SCENARIO_SUFFIXES = (".scenario.yaml", ".scenario.yml")


class RenderSetValidationError(ValueError):
    """The manifest or one of its referenced scenarios is unsafe/inconsistent."""


@dataclass(frozen=True, slots=True)
class RenderSetVariantPlan:
    """Resolved non-secret paths for one validated language variant."""

    language: str
    scenario: Path
    output: Path


@dataclass(frozen=True, slots=True)
class RenderSetPlan:
    """Validated ordered render-set plan."""

    source: Path
    variants: tuple[RenderSetVariantPlan, ...]
    provider: str


def _portable_relative_path(
    raw: str,
    *,
    field: str,
    suffixes: tuple[str, ...],
) -> Path:
    if "\\" in raw:
        raise RenderSetValidationError(f"{field} musi używać przenośnych separatorów `/`")
    portable = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if portable.is_absolute() or windows.drive or ".." in portable.parts:
        raise RenderSetValidationError(f"{field} musi być ścieżką względną bez `..`: {raw}")
    if ":" in raw:
        raise RenderSetValidationError(f"{field} musi używać przenośnych separatorów `/`")
    if not portable.parts or str(portable) in {"", "."}:
        raise RenderSetValidationError(f"{field} nie może być pustą ścieżką")
    if not any(raw.endswith(suffix) for suffix in suffixes):
        expected = " lub ".join(suffixes)
        raise RenderSetValidationError(f"{field} musi kończyć się na {expected}: {raw}")
    return Path(*portable.parts)


def load_render_set(
    path: Path | str,
    env: Mapping[str, str] | None = None,
) -> RenderSetPlan:
    """Validate a manifest and every scenario before browser/output mutation."""

    path = Path(path).resolve()
    yaml = YAML(typ="safe")
    manifest = LocalizedRenderSet.model_validate(yaml.load(path.read_text(encoding="utf-8")))
    manifest_key = str(path).casefold()

    variants: list[RenderSetVariantPlan] = []
    scenario_keys: set[str] = set()
    compiled_keys: set[str] = set()
    output_keys: set[str] = set()
    providers: set[str] = set()
    for language, declared in manifest.variants.items():
        relative_scenario = _portable_relative_path(
            declared.scenario,
            field=f"variants.{language}.scenario",
            suffixes=_SCENARIO_SUFFIXES,
        )
        relative_output = _portable_relative_path(
            declared.output,
            field=f"variants.{language}.output",
            suffixes=(".mp4",),
        )
        scenario_path = (path.parent / relative_scenario).resolve()
        try:
            scenario_path.relative_to(path.parent)
        except ValueError:
            raise RenderSetValidationError(
                f"variants.{language}.scenario wychodzi poza katalog manifestu po rozwiązaniu "
                "linków symbolicznych"
            ) from None
        scenario_key = str(scenario_path).casefold()
        sidecar_path = compiled_path(scenario_path)
        compiled_key = str(sidecar_path.resolve()).casefold()
        output_key = relative_output.as_posix().casefold()
        if scenario_key in scenario_keys:
            raise RenderSetValidationError(
                f"scenariusz wariantu {language} jest użyty więcej niż raz: {declared.scenario}"
            )
        if output_key in output_keys:
            raise RenderSetValidationError(
                f"output wariantu {language} koliduje z innym wariantem: {declared.output}"
            )
        if compiled_key in compiled_keys:
            raise RenderSetValidationError(
                f"sidecar kompilacji wariantu {language} koliduje z innym wariantem: "
                f"{compiled_path(relative_scenario).as_posix()}"
            )
        if manifest_key == compiled_key:
            raise RenderSetValidationError(
                f"sidecar kompilacji wariantu {language} nadpisałby manifest zestawu"
            )
        scenario_keys.add(scenario_key)
        compiled_keys.add(compiled_key)
        output_keys.add(output_key)

        try:
            scenario = load_scenario(scenario_path, env)
        except ScenarioValidationError:
            # Banner jest bezpieczny do pokazania w całości: snippet pochodzi
            # sprzed podstawienia ${ENV}, a treść składa się wyłącznie z nazw
            # kluczy — inaczej niż surowe `pydantic.ValidationError`, które
            # niesie `input_value=…`, czyli wartości już podstawione. Ścieżka
            # w nagłówku sama wskazuje wariant, więc nic nie trzeba doklejać.
            raise
        except Exception as exc:
            raise RenderSetValidationError(
                f"wariant {language}: nie można wczytać {declared.scenario} "
                f"({type(exc).__name__}); sprawdź poprawność scenariusza"
            ) from None
        cfg = scenario.config
        if cfg.locale != language:
            raise RenderSetValidationError(
                f"wariant {language}: config.locale musi być równe kluczowi wariantu"
            )
        if cfg.tts.lang != language:
            raise RenderSetValidationError(
                f"wariant {language}: config.tts.lang musi być równe kluczowi wariantu"
            )
        if cfg.tts.track_language is None:
            raise RenderSetValidationError(
                f"wariant {language}: config.tts.trackLanguage jest wymagane"
            )
        if cfg.audio_tracks:
            raise RenderSetValidationError(
                f"wariant {language}: render-set wymaga dokładnie jednej ścieżki; "
                "usuń config.audioTracks"
            )
        providers.add(cfg.tts.provider)
        variants.append(
            RenderSetVariantPlan(
                language=language,
                scenario=scenario_path,
                output=relative_output,
            )
        )

    if len(providers) != 1:
        raise RenderSetValidationError(
            "wszystkie warianty render-set muszą używać jednego providera TTS; "
            f"skonfigurowano: {', '.join(sorted(providers))}"
        )
    return RenderSetPlan(source=path, variants=tuple(variants), provider=providers.pop())
