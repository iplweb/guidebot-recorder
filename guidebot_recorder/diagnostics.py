"""Bannery diagnostyczne: `plik:linia` + dosłowny fragment YAML (styl Ansible).

`render_banner` to jedyne miejsce, które zna format (numery linii, wcięcia,
karetkę, ucięcie). Funkcje wyższego poziomu — `step_banner` (komunikaty
runtime) i `validation_banner` (błędy walidacji) — składają tylko nagłówek,
a `step_banner` dodatkowo redaguje gotowy banner.

Moduł celowo nie importuje `guidebot_recorder.scenario.source` w czasie
wykonania: `StepLocation` i `ScenarioSource` są używane wyłącznie jako
adnotacje, a w runtime liczy się sam kształt obiektu (duck typing).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from guidebot_recorder.recorder._debug import redact_text

if TYPE_CHECKING:
    from guidebot_recorder.scenario.source import ScenarioSource, StepLocation

#: Szerokość pola numeru linii; z separatorem daje 10 znaków przed treścią.
_NUMBER_WIDTH = 7
_SEPARATOR = " | "
#: Wcięcie treści komunikatu (trzy spacje).
_MESSAGE_INDENT = "   "
#: Karetka stoi w kolumnie 11 — pierwszy znak treści linii w snippecie.
_CARET_LINE = " " * (_NUMBER_WIDTH + len(_SEPARATOR)) + "^ tutaj"
#: Wiersz elipsy nie ma numeru linii, więc dosuwamy go do pola numeru.
_ELLIPSIS_INDENT = " " * 8
_VALIDATION_HEADLINE = "BŁĄD walidacji"


def render_banner(
    headline: str,
    snippet: list[tuple[int, str]],
    message: str,
    *,
    caret_line: int | None = None,
    max_lines: int = 8,
) -> str:
    """Złóż banner: nagłówek, ponumerowany fragment źródła i wcięta treść.

    `snippet` to pary `(numer_linii, dosłowna_treść)` — pełny span kroku;
    ucięcie do `max_lines` (plus wiersze elipsy) robi wyłącznie ta funkcja.

    Okno zaczyna się od początku snippetu, ale gdy `caret_line` wypadłaby poza
    nie — przesuwa się tak, żeby wyśrodkować winną linię. Karetka bez swojej
    linii jest bezużyteczna, a snippet bez winnej linii wprowadza w błąd.
    """

    start = _window_start(snippet, caret_line, max_lines)
    visible = snippet[start : start + max_lines] if max_lines > 0 else []

    lines = [headline]
    if start > 0:
        lines.append(f"{_ELLIPSIS_INDENT}… (wcześniej {_count_of_lines(start)})")
    for number, text in visible:
        lines.append(f"{number:>{_NUMBER_WIDTH}}{_SEPARATOR}{text}")
        if caret_line is not None and number == caret_line:
            lines.append(_CARET_LINE)
    hidden = len(snippet) - start - len(visible)
    if hidden > 0:
        lines.append(f"{_ELLIPSIS_INDENT}… (jeszcze {_count_of_lines(hidden)})")
    lines.extend(_indented(message))
    return "\n".join(lines)


def _window_start(snippet: list[tuple[int, str]], caret_line: int | None, max_lines: int) -> int:
    """Indeks pierwszej pokazywanej linii snippetu — 0, chyba że karetka wypada dalej."""

    if max_lines <= 0 or caret_line is None or len(snippet) <= max_lines:
        return 0
    caret_index = next((i for i, (number, _) in enumerate(snippet) if number == caret_line), None)
    if caret_index is None or caret_index < max_lines:
        return 0
    centred = caret_index - (max_lines - 1) // 2
    return min(centred, len(snippet) - max_lines)


def _count_of_lines(count: int) -> str:
    """`count` z odmienionym rzeczownikiem: 1 linia, 2 linie, 5 linii, 22 linie, 112 linii."""

    return f"{count} {_lines_noun(count)}"


def _lines_noun(count: int) -> str:
    """Forma rzeczownika „linia" dla liczebnika `count` (polska odmiana przez przypadki)."""

    if count == 1:
        return "linia"
    if 12 <= count % 100 <= 14:
        return "linii"
    return "linie" if count % 10 in (2, 3, 4) else "linii"


def step_banner(
    *,
    index: int,
    total: int,
    location: StepLocation | None,
    source: ScenarioSource | None,
    message: str,
    warning: bool = False,
    sensitive: Iterable[str] = (),
) -> str:
    """Banner komunikatu runtime dla kroku o płaskim indeksie `index` (0-based).

    Nagłówek jest 1-based: `krok {index + 1}/{total}`. Bez lokalizacji lub bez
    źródła degraduje się do samego `krok n/total` i treści — nigdy nie rzuca.

    `sensitive` przepuszcza **cały** banner przez `redact_text`: treść
    komunikatu bywa sklejana już po podstawieniu `${ENV}`, więc bez tego sekret
    wyciekłby wierszem pod „bezpiecznym" snippetem.
    """

    headline = f"{'⚠ ' if warning else ''}krok {index + 1}/{total}"
    snippet: list[tuple[int, str]] = []
    if location is not None and source is not None:
        headline = f"{headline} — {source.path}:{location.line}{_step_suffix(location)}"
        snippet = _safely(lambda: source.snippet(location), [])
    return redact_text(render_banner(headline, snippet, message), sensitive)


def validation_banner(
    *,
    source: ScenarioSource | None,
    line: int | None,
    index: int | None,
    total: int,
    message: str,
) -> str:
    """Banner błędu walidacji — pierwszorzędna jest linia, numer kroku to kontekst.

    Gdy `index` jest znany, snippet obejmuje cały span kroku; gdy linia nie
    należy do żadnego kroku (`index is None`) — samą tę linię. Karetka zawsze
    wskazuje `line`.
    """

    if source is None:
        return render_banner(_VALIDATION_HEADLINE, [], message)
    if line is None:
        return render_banner(f"{_VALIDATION_HEADLINE} — {source.path}", [], message)

    headline = f"{_VALIDATION_HEADLINE} — {source.path}:{line}"
    snippet: list[tuple[int, str]] = []
    if index is not None:
        headline = f"{headline} (krok {index + 1}/{total})"
        location = _safely(lambda: source.location(index), None)
        if location is not None:
            snippet = _safely(lambda: source.snippet(location), [])
    if not snippet:
        snippet = _safely(lambda: source.line_snippet(line), [])
    return render_banner(headline, snippet, message, caret_line=line)


def _step_suffix(location: StepLocation) -> str:
    """Człon nagłówka mówiący o przynależności kroku do bloku `when:`."""

    if location.is_gate:
        return " (bramka `when:`)"
    if location.gate_line is not None:
        return f" (w bramce z linii {location.gate_line})"
    return ""


def _indented(message: str) -> list[str]:
    """Treść komunikatu wcięta trzema spacjami w każdej linii (puste zostają puste)."""

    return [f"{_MESSAGE_INDENT}{line}" if line.strip() else "" for line in message.splitlines()]


def _safely[T](build: Callable[[], T], default: T) -> T:
    """Uruchom `build`, a przy dowolnym błędzie zwróć `default`.

    Bannery powstają w trakcie obsługi innego błędu — niekompletne czy
    niespójne źródło nie może wyprodukować wyjątku z wyjątku.
    """

    try:
        return build()
    except Exception:  # noqa: BLE001 — banner nie ma prawa przesłonić właściwego błędu
        return default
