"""Human-facing text produced by the compile phase: descriptions and warnings.

Everything here turns a scenario object into something a person reads — the
``--verbose`` one-liners, the target description, and the two ``tqdm.write``
banners. Grouped by that audience rather than by the step kinds they happen to
describe: none of it touches Playwright, so it stays testable without a browser
and importable without a cycle.

:func:`_resolve_url` sits here for the same reason — it turns a scenario's
relative ``navigate`` into the absolute URL the log and the browser both use, and
needs nothing but the scenario.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urljoin

from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    Target,
    TestidTarget,
    TextTarget,
)
from guidebot_recorder.resolver.positional import Pinned
from guidebot_recorder.resolver.resolution import (
    step_instruction as _instruction,
)
from guidebot_recorder.scenario.source import ScenarioSource, StepLocation


def _resolve_url(scenario: Scenario, url: str) -> str:
    base = scenario.config.base_url
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


def _short(step: Step, limit: int = 60) -> str:
    """Short, readable step description for the verbose log."""
    for attr in ("say", "teach", "navigate", "click", "hover"):
        value = step.navigate_url() if attr == "navigate" else getattr(step, attr)
        if value:
            text = str(value)
            return text if len(text) <= limit else text[: limit - 1] + "…"
    if step.close_window is not None:
        return "closeWindow"
    if step.slide is not None:
        return step.slide.title or step.slide.subtitle or "slide"
    if step.desktop is not None:
        return f"desktop: {step.desktop.icon}"
    if step.enter_text is not None:
        return f"→ {step.enter_text.into}"
    if step.highlight is not None:
        return f"◯ {step.highlight.what}"
    if step.wait is not None:
        return step.wait.until if isinstance(step.wait, WaitUntil) else f"{step.wait}s"
    return ""


def _target_desc(target: Target) -> str:
    """Opis namiaru dla `--verbose`, z `scope` i `nth`.

    Te dwa pola są dokładnie tym, co odróżnia jeden element od kilku identycznych
    z nim rodzeństwa — log, który je pomija, ukrywa całą treść namiaru
    pozycyjnego i pokazuje dwa różne kroki jako ten sam target.
    """

    if isinstance(target, RoleTarget):
        desc = f'role={target.role} name="{target.name}"'
        if target.nth is not None:
            desc = f"{desc} nth={target.nth}"
    elif isinstance(target, TextTarget):
        desc = f'text="{target.text}"'
    elif isinstance(target, LabelTarget):
        desc = f'label="{target.label}"'
    elif isinstance(target, TestidTarget):
        desc = f"testid={target.testid}"
    else:  # pragma: no cover - the union is closed
        return str(target)
    if target.scope is not None:
        desc = f"{desc} scope=[{_target_desc(target.scope)}]"
    return desc


def _warn_absent(
    index: int,
    step: Step,
    *,
    gate: bool,
    total: int,
    location: StepLocation | None = None,
    source: ScenarioSource | None = None,
    sensitive: Iterable[str] = (),
) -> None:
    """Ostrzeż o nieobecnym elemencie opcjonalnym — banner z `plik:linia`.

    Instrukcja kroku bywa dosłowną kopią wartości wstrzykniętej przez `${ENV}`,
    więc `sensitive` nie jest ozdobnikiem: bez niego sekret wyciekłby wierszem
    pod „bezpiecznym" fragmentem YAML-a.
    """

    what = "element bramkujący" if gate else "element opcjonalny"
    tqdm.write(
        step_banner(
            index=index,
            total=total,
            location=location,
            source=source,
            message=(
                f"{what} {_instruction(step)!r} nie pojawił się — "
                "zapisano wpis oczekujący (pending); render rozwiąże go na miejscu"
            ),
            warning=True,
            sensitive=sensitive,
        )
    )


def _warn_positional(
    index: int,
    pinned: Pinned,
    *,
    total: int,
    location: StepLocation | None = None,
    source: ScenarioSource | None = None,
    sensitive: Iterable[str] = (),
) -> None:
    """Ostrzeż, że krok trafił w cel dopiero po zmierzeniu indeksu — z `plik:linia`.

    Indeks jest teraz mierzony, nie zgadywany, ale zostaje kruchy: jednorodne
    dołożenie rodzeństwa przesuwa go cicho, a wykrywanie dryfu tego wariantu
    z założenia nie łapie (spec: „Ograniczenie: co ten sygnał łapie, a czego
    nie"). Dlatego komunikat kieruje autora do doprecyzowania opisu — czyli
    tam, gdzie problem znika na dobre — zamiast obiecywać, że mechanizm sam się
    obroni.

    Liczebnik jest 1-based, bo to zdanie dla człowieka: „1 z 2" znaczy po polsku
    „pierwszy z dwóch", więc wydrukowanie tu surowego `nth=1` opisywałoby drugie
    trafienie słowami pierwszego. Surowa wartość idzie zaraz obok, w nawiasie —
    bez niej autor nie skoreluje banera z `nth` w sidecarze ani w `--verbose`,
    a z nią nie musi wybierać między czytelnością a możliwością odnalezienia
    wpisu. Ostatnia liczba to trafienia namiaru bez `nth`.

    `sensitive` przechodzi przez banner jak w :func:`_warn_absent`: sam komunikat
    jest złożony z liczb, ale fragment YAML-a pod nim cytuje krok, który bywa
    sklejony z `${ENV}`.
    """

    tqdm.write(
        step_banner(
            index=index,
            total=total,
            location=location,
            source=source,
            message=(
                f"namiar pozycyjny ({pinned.index + 1} z {pinned.matches} pasujących, "
                f"nth={pinned.index}) — rozważ doprecyzowanie opisu, żeby wskazywał "
                "element jednoznacznie"
            ),
            warning=True,
            sensitive=sensitive,
        )
    )
