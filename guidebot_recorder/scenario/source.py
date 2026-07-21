"""Mapa źródła scenariusza: gdzie w YAML-u mieszka który krok.

Drugi, *round-trip* parse tego samego tekstu, który trafia do ``_parse_source``
(a więc **sprzed** podstawienia ``${ENV}`` — w snippecie widać ``${HASŁO}``,
nigdy hasła). Z węzłów ruamela (``.lc``) czytamy pozycje i sprowadzamy je do
spanów kroków płaskich, indeksowanych dokładnie tak samo jak
``Scenario.flat_steps()``: bramka bloku ``when:``, potem jego dzieci, potem
następny wpis listy ``steps:``.

Moduł jest **totalny**: ``build_source`` woła się przed walidacją, więc dostaje
także pliki niepoprawne i składniowo zepsute. Każdy taki plik daje częściowy
albo pusty :class:`ScenarioSource` — nigdy wyjątek. Diagnostyka nie ma prawa
wysypać się na pliku, o którym właśnie miała powiedzieć, co jest z nim nie tak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


@dataclass(frozen=True)
class StepLocation:
    """Gdzie krok płaski mieszka w źródłowym YAML-u (linie 1-based, obustronnie domknięte)."""

    #: pierwsza linia kroku
    line: int
    #: ostatnia linia kroku
    end_line: int
    #: syntetyczny krok bramkujący bloku ``when:``
    is_gate: bool
    #: linia ``when:`` bloku-właściciela; ``None`` dla kroku top-level.
    #: Dla samej bramki równa ``line`` — bramka należy do swojego bloku.
    gate_line: int | None


@dataclass(frozen=True)
class ScenarioSource:
    """Surowe linie pliku plus spany kroków, indeksowane płaskim indeksem kroku."""

    path: Path
    #: linie pliku, bez znaku końca linii
    lines: tuple[str, ...]
    #: spany kroków w kolejności ``flat_steps()``
    steps: tuple[StepLocation, ...]
    #: drzewo round-trip (dla :meth:`node_line`); poza porównaniem i reprezentacją
    document: Any = field(default=None, compare=False, repr=False)

    def location(self, index: int) -> StepLocation | None:
        """Span kroku o płaskim indeksie ``index`` albo ``None``, gdy go nie znamy."""

        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def snippet(self, loc: StepLocation) -> list[tuple[int, str]]:
        """PEŁNY span jako pary ``(numer_linii, treść)``; ucięcie robi ``render_banner``."""

        return [
            (number, self.lines[number - 1])
            for number in range(loc.line, loc.end_line + 1)
            if 1 <= number <= len(self.lines)
        ]

    def line_snippet(self, line: int) -> list[tuple[int, str]]:
        """Pojedyncza linia jako snippet; pusta lista, gdy linii nie ma w pliku."""

        if 1 <= line <= len(self.lines):
            return [(line, self.lines[line - 1])]
        return []

    def node_line(self, loc_path: tuple[str | int, ...]) -> int | None:
        """Linia węzła wskazanego ścieżką ``loc`` pydantica.

        Elementy nieadresowalne (tagi wariantów unii, klucze nieobecne w źródle)
        **pomijamy i idziemy dalej** tą samą ścieżką — nie przerywamy marszu.
        Inaczej błąd dziecka bloku (``('steps', 1, 'WhenBlock', 'steps', 0)``)
        zmapowałby się na linię bloku zamiast dziecka.

        Zwraca linię najgłębszego trafionego węzła; ``None``, gdy nie
        skonsumowano niczego.
        """

        node: Any = self.document
        line: int | None = None
        for part in loc_path:
            found = _child_line(node, part)
            if found is None:
                continue
            line, node = found
        return line

    def index_at_line(self, line: int) -> int | None:
        """Płaski indeks kroku obejmującego ``line``.

        ``None`` dla linii niczyich: komentarzy i pustych linii *między* krokami
        (przycinanie spanów zostawia luki) oraz całej sekcji ``config:``.
        """

        for index, loc in enumerate(self.steps):
            if loc.line <= line <= loc.end_line:
                return index
        return None


@lru_cache(maxsize=32)
def build_source(path: Path, text: str) -> ScenarioSource:
    """Zbuduj mapę źródła dla ``text`` spod ``path``. Nigdy nie rzuca.

    Cache po parze ``(path, text)``, nie po samym tekście: identyczna treść pod
    dwiema ścieżkami musi dać dwa różne ``ScenarioSource.path``.
    """

    lines = tuple(text.splitlines())
    try:
        document = YAML().load(text)
    except Exception:  # noqa: BLE001 — zepsuta składnia to zadanie `_parse_source`
        document = None
    return ScenarioSource(
        path=path,
        lines=lines,
        steps=tuple(_step_locations(document, lines)),
        document=document,
    )


def _child_line(node: Any, part: str | int) -> tuple[int, Any] | None:
    """``(linia, dziecko)`` dla adresowalnego elementu ścieżki, inaczej ``None``."""

    try:
        if isinstance(node, CommentedMap) and part in node:
            return node.lc.key(part)[0] + 1, node[part]
        if isinstance(node, CommentedSeq) and isinstance(part, int) and 0 <= part < len(node):
            return node.lc.item(part)[0] + 1, node[part]
    except Exception:  # noqa: BLE001 — brak pozycji w .lc traktujemy jak brak węzła
        return None
    return None


def _key_line(node: Any, key: str) -> int | None:
    found = _child_line(node, key)
    return found[0] if found else None


def _item_line(node: Any, index: int) -> int | None:
    found = _child_line(node, index)
    return found[0] if found else None


def _is_filler(line: str) -> bool:
    """Linia pusta albo zawierająca wyłącznie komentarz."""

    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _trim(lines: tuple[str, ...], start: int, end: int) -> int:
    """Przytnij koniec spanu z pustych linii i komentarzy; nigdy poniżej ``start``."""

    end = min(end, len(lines))
    while end > start and 1 <= end <= len(lines) and _is_filler(lines[end - 1]):
        end -= 1
    return max(end, start)


def _steps_block_end(root: Any, lines: tuple[str, ...]) -> int:
    """Ostatnia linia należąca do listy ``steps:`` — przed następnym kluczem top-level."""

    steps_line = _key_line(root, "steps")
    following = [
        line
        for key in root
        if (line := _key_line(root, key)) is not None
        and steps_line is not None
        and line > steps_line
    ]
    return min(following) - 1 if following else len(lines)


def _step_locations(document: Any, lines: tuple[str, ...]) -> list[StepLocation]:
    """Spany kroków w kolejności ``flat_steps()``; przy każdej wątpliwości pomijamy wpis."""

    if not isinstance(document, CommentedMap):
        return []
    steps = document.get("steps")
    if not isinstance(steps, CommentedSeq):
        return []

    block_end = _steps_block_end(document, lines)
    locations: list[StepLocation] = []
    for index, entry in enumerate(steps):
        start = _item_line(steps, index)
        if start is None:
            continue
        next_start = _item_line(steps, index + 1) if index + 1 < len(steps) else None
        end = _trim(lines, start, next_start - 1 if next_start else block_end)

        if isinstance(entry, CommentedMap) and "when" in entry:
            locations.extend(_block_locations(entry, lines, start, end))
        else:
            locations.append(StepLocation(line=start, end_line=end, is_gate=False, gate_line=None))
    return locations


def _block_locations(
    entry: CommentedMap, lines: tuple[str, ...], start: int, end: int
) -> list[StepLocation]:
    """Bramka bloku ``when:`` plus jego dzieci — w kolejności wykonania.

    Span bramki kończy się linię przed kluczem ``steps:`` bloku; klucze zapisane
    *po* liście dzieci nie trafią do snippetu bramki — świadome uproszczenie na
    rzecz ciągłego fragmentu.
    """

    children_key_line = _key_line(entry, "steps")
    gate_end = end if children_key_line is None else _trim(lines, start, children_key_line - 1)
    locations = [StepLocation(line=start, end_line=gate_end, is_gate=True, gate_line=start)]

    children = entry.get("steps")
    if not isinstance(children, CommentedSeq):
        return locations

    for index, _child in enumerate(children):
        child_start = _item_line(children, index)
        if child_start is None:
            continue
        next_start = _item_line(children, index + 1) if index + 1 < len(children) else None
        child_end = _trim(lines, child_start, next_start - 1 if next_start else end)
        locations.append(
            StepLocation(line=child_start, end_line=child_end, is_gate=False, gate_line=start)
        )
    return locations
