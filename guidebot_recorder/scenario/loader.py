"""Load a scenario YAML into a validated ``Scenario`` (with ``${ENV}`` expanded).

The source file is read-only: resolved actions are written to a separate
``*.compiled.yaml`` (see ``scenario.compiled``), so nothing is ever written back
here. A round-trip parse is kept all the same — read-only, in
:class:`~guidebot_recorder.scenario.source.ScenarioSource` — solely to tell
diagnostics which line of the file a step lives on.
``${ENV}`` substitution (§3.2) is applied only while building the model; a missing
variable raises ``KeyError``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML

from guidebot_recorder.diagnostics import validation_banner
from guidebot_recorder.models.scenario import Scenario, StepPathError
from guidebot_recorder.scenario.env import referenced_env_names, substitute_scenario_values
from guidebot_recorder.scenario.source import ScenarioSource, build_source

#: Prefiks, którym pydantic opatruje komunikaty z ``raise ValueError`` w walidatorach.
_VALUE_ERROR_PREFIX = "Value error, "


class ScenarioValidationError(ValueError):
    """Błąd walidacji scenariusza, przetłumaczony na `plik:linia` + fragment YAML.

    Dziedziczy po ``ValueError`` (jak ``pydantic.ValidationError``, którego
    zastępuje), więc wszystkie zastane ``except Exception`` / ``pytest.raises(ValueError)``
    działają bez zmian.
    """


class CompiledSidecarError(ValueError):
    """Raised when a ``*.compiled.yaml`` sidecar is passed where a source scenario is expected.

    The generated sidecar is an input only to ``render`` (and even there the
    ``.scenario.yaml`` is passed, with the sidecar found beside it) — never to
    ``compile``/``validate``. Its top-level shape is ``compiler_version``/``source``/
    ``actions``, so validating it as a :class:`Scenario` would otherwise spew a
    confusing pydantic error about missing ``config``/``steps``.
    """


def _looks_like_compiled_sidecar(raw: object, path: Path) -> bool:
    """Heuristic: a compiled sidecar by filename, or by its distinctive top-level shape."""

    if path.name.endswith(".compiled.yaml"):
        return True
    return (
        isinstance(raw, Mapping)
        and "config" not in raw
        and "steps" not in raw
        and ("compiler_version" in raw or "actions" in raw)
    )


def _sidecar_message(path: Path) -> str:
    source = re.sub(r"\.compiled\.yaml$", ".scenario.yaml", path.name)
    hint = f"`{source}`" if source != path.name else "plik `*.scenario.yaml`"
    return (
        f"`{path.name}` to skompilowany sidecar (*.compiled.yaml), nie scenariusz źródłowy. "
        f"Podaj plik scenariusza, np. {hint}. Sidecar jest wynikiem `compile` i wejściem "
        f"tylko do `render` (i tam też podaje się `.scenario.yaml`)."
    )


def guard_source_scenario(path: Path | str) -> None:
    """Fail fast with :class:`CompiledSidecarError` if ``path`` is a compiled sidecar.

    A cheap check (raw parse only, no env substitution or full validation) so
    callers can reject the mistake with a friendly message before the heavier
    pipeline — or a missing-``${ENV}`` error — has a chance to obscure it.
    """

    path = Path(path)
    if _looks_like_compiled_sidecar(_read_raw(path), path):
        raise CompiledSidecarError(_sidecar_message(path))


def _to_plain(node):
    """Reduce a ruamel structure to plain Python types (for pydantic)."""
    if isinstance(node, Mapping):
        return {str(k): _to_plain(v) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_to_plain(v) for v in node]
    if isinstance(node, bool):
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if node is None:
        return None
    return str(node)


@lru_cache(maxsize=32)
def _parse_source(source: str) -> dict:
    """Parse unchanged source text once; callers never mutate the cached value."""

    yaml = YAML(typ="safe")
    data = yaml.load(source)
    return _to_plain(data)


def _read_raw(path: Path) -> dict:
    """Read the scenario YAML into plain Python types (``${ENV}`` still intact).

    The file is read on every call, so edits are observed immediately. Only the
    deterministic YAML parse is shared when the exact source text is unchanged;
    environment substitution still receives a deep copy in
    :func:`substitute_scenario_values`.
    """

    return _parse_source(path.read_text(encoding="utf-8"))


def load_scenario(path: Path | str, env: Mapping[str, str] | None = None) -> Scenario:
    """Load and validate the source scenario at ``path`` (``env`` defaults to os.environ)."""
    if env is None:
        env = os.environ
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    raw = _parse_source(text)
    if _looks_like_compiled_sidecar(raw, path):
        raise CompiledSidecarError(_sidecar_message(path))
    # zbudowane z surowego tekstu, sprzed podstawienia ${ENV}: w snippecie
    # diagnostyki widać `${HASŁO}`, nigdy samego hasła
    source = build_source(path, text)
    if not isinstance(raw, Mapping):
        # przed podstawieniem: `substitute_scenario_values` woła `raw.get(...)`,
        # więc pusty (albo nie-mapowy) plik wysypywałby się `AttributeError`-em
        # jeszcze zanim diagnostyka zdążyłaby powiedzieć, o który plik chodzi
        raise ScenarioValidationError(
            validation_banner(
                source=source,
                line=None,
                index=None,
                total=len(source.steps),
                message=(
                    "plik nie zawiera mapy YAML — oczekiwano kluczy `config:` i `steps:` "
                    "na najwyższym poziomie (plik jest pusty albo to nie scenariusz)"
                ),
            )
        )
    substituted = substitute_scenario_values(raw, env)
    try:
        scenario = Scenario.model_validate(substituted)
    except ValidationError as exc:
        raise ScenarioValidationError(format_validation_error(exc, source, raw)) from None
    scenario.attach_source(source)
    return scenario


def format_validation_error(
    exc: ValidationError, source: ScenarioSource | None, raw: object
) -> str:
    """Zamień błędy pydantica na bannery `plik:linia` + fragment YAML.

    ``raw`` to surowa (sprzed podstawienia ``${ENV}``) postać dokumentu —
    potrzebna wyłącznie do rozstrzygnięcia, który wariant unii ``Step | WhenBlock``
    autor miał na myśli. Wiele ocalałych błędów → bannery sklejone pustą linią,
    w kolejności z ``exc.errors()``.
    """

    total = len(source.steps) if source is not None else 0
    banners: list[str] = []
    seen: set[tuple[int | None, str]] = set()
    for error in _relevant_errors(exc.errors(), raw):
        line = _error_line(error, source)
        message = _error_message(error)
        if (line, message) in seen:
            continue
        seen.add((line, message))
        index = source.index_at_line(line) if source is not None and line is not None else None
        banners.append(
            validation_banner(source=source, line=line, index=index, total=total, message=message)
        )
    if not banners:  # nie powinno się zdarzyć — ale banner bez treści byłby gorszy
        # Świadomie BEZ `str(exc)`: pydantic dokłada tam `input_value=…`, czyli
        # wartość **po** podstawieniu `${ENV}` — a bannery mają nie znać sekretów.
        banners.append(
            validation_banner(
                source=source,
                line=None,
                index=None,
                total=total,
                message="scenariusz nie przeszedł walidacji (brak szczegółów do przypisania)",
            )
        )
    return "\n\n".join(banners)


def _relevant_errors(errors: list[dict], raw: object) -> list[dict]:
    """Odsiej błędy z odrzuconego wariantu unii ``Step | WhenBlock``.

    Jeden błąd autora daje kilka wpisów w ``exc.errors()``: krok z dwiema
    komendami — pięć (cztery z nich to „missing when", „extra_forbidden click"…
    z niedopasowanego ``WhenBlock``). Bez filtra użytkownik dostałby banner
    „Extra inputs are not permitted: when" na kroku, który *ma* być blokiem
    ``when:``.
    """

    groups: dict[int, list[dict]] = {}
    order: list[tuple[int, dict] | tuple[None, dict]] = []
    for error in errors:
        index = _step_index(error["loc"])
        if index is None:
            order.append((None, error))
            continue
        if index not in groups:
            groups[index] = []
            order.append((index, error))
        groups[index].append(error)

    relevant: list[dict] = []
    for index, error in order:
        if index is None:
            relevant.append(error)
        else:
            relevant.extend(_pick_union_variant(groups[index], _entry_is_block(raw, index)))
    return relevant


def _step_index(loc: tuple) -> int | None:
    """Pozycja w ``steps:``, do której należy błąd; ``None`` dla błędów spoza listy."""

    if len(loc) >= 2 and loc[0] == "steps" and isinstance(loc[1], int):
        return loc[1]
    return None


def _entry_is_block(raw: object, index: int) -> bool:
    """Czy autor napisał tam blok ``when:`` (a więc czy chciał wariantu ``WhenBlock``)."""

    steps = raw.get("steps") if isinstance(raw, Mapping) else None
    if not isinstance(steps, list) or not 0 <= index < len(steps):
        return False
    entry = steps[index]
    return isinstance(entry, Mapping) and "when" in entry


def _pick_union_variant(group: list[dict], is_block: bool) -> list[dict]:
    """Zostaw w grupie wyłącznie błędy z wariantu, który autor miał na myśli.

    Gdyby filtr wyczyścił grupę do zera (nieoczekiwany kształt tagu), zostawiamy
    ją w całości — lepiej nadmiar niż zgubiony błąd.
    """

    kept = [error for error in group if _is_when_variant(error) is is_block]
    return kept or list(group)


def _is_when_variant(error: dict) -> bool:
    """Czy błąd pochodzi z wariantu ``WhenBlock`` (tag unii siedzi w ``loc[2]``).

    Tag wariantu ``Step`` to realnie ``'function-after[_optional_only_where_it_can_be_honoured(),
    function-after[_exactly_one_command(), Step]]'`` — zawiera ``"Step"``, nie
    zawiera ``"WhenBlock"``, więc reguła separuje warianty poprawnie.
    """

    loc = error["loc"]
    tag = loc[2] if len(loc) > 2 else None
    return isinstance(tag, str) and "WhenBlock" in tag


def _error_message(error: dict) -> str:
    """Treść błędu bez pydantikowego prefiksu, z dopisaną ścieżką pola.

    Komunikat walidatora (``value_error``) jest samowystarczalny; „Field required"
    czy „Extra inputs are not permitted" bez nazwy pola nie znaczy nic, więc
    dostają sufiks w rodzaju ``: enterText.text``.
    """

    message = error["msg"]
    if error["type"] == "value_error":
        return message.removeprefix(_VALUE_ERROR_PREFIX)
    field = _field_path(error["loc"])
    return f"{message}: {field}" if field else message


def _field_path(loc: tuple) -> str:
    """Ścieżka pola w kropkowej notacji, bez prefiksu kroku i bez tagu wariantu unii."""

    parts = loc[3:] if _step_index(loc) is not None else loc
    return ".".join(str(part) for part in parts)


def _error_line(error: dict, source: ScenarioSource | None) -> int | None:
    """Linia w źródle, o którą chodzi — z ``loc`` albo (mocniej) ze ``StepPathError``."""

    if source is None:
        return None
    line = source.node_line(tuple(error["loc"]))
    origin = error.get("ctx", {}).get("error")
    if isinstance(origin, StepPathError):
        # jedyna droga do walidatorów poziomu `Scenario`, które mają `loc == ()`
        override = source.node_line(_positional_path(origin.path))
        if override is not None:
            line = override
    return line


def _positional_path(path: tuple[int, ...]) -> tuple[str | int, ...]:
    """``(3, 1)`` → ``('steps', 3, 'steps', 1)`` — ścieżka pozycyjna w drzewie źródła."""

    parts: list[str | int] = []
    for position in path:
        parts.extend(("steps", position))
    return tuple(parts)


def scenario_env_references(
    path: Path | str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return ``{name: env[name]}` for env vars the scenario references via ``${VAR}``.

    Only these values were expanded into navigation URLs / typed text, so only
    they are candidate secrets for redaction (see ``referenced_env_names``).
    """
    if env is None:
        env = os.environ
    names = referenced_env_names(_read_raw(Path(path)))
    return {name: env[name] for name in names if name in env}
