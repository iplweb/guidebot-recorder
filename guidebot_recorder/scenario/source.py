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

Nie każdy krok da się zlokalizować. Dla aliasu (``- *consent``) i merge key
(``<<: *base``) ruamel zwraca pozycję **definicji kotwicy**, nie miejsca użycia —
span cofnąłby się i pociągnął cudze linie. Takie kroki dostają placeholder
``None``, który **zajmuje pozycję na liście**: ``len(steps)`` zawsze równa się
``len(Scenario.flat_steps())``, a banner degraduje do samego ``krok n/total``.
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
    #: spany kroków w kolejności ``flat_steps()``; ``None`` dla kroku, którego
    #: pozycji nie da się wiarygodnie ustalić (alias, merge key) — placeholder
    #: zajmuje pozycję, żeby indeksy zgadzały się z ``flat_steps()``
    steps: tuple[StepLocation | None, ...]
    #: drzewo round-trip (dla :meth:`node_line`); poza porównaniem i reprezentacją
    document: Any = field(default=None, compare=False, repr=False)

    def location(self, index: int) -> StepLocation | None:
        """Span kroku o płaskim indeksie ``index`` albo ``None``, gdy go nie znamy.

        ``None`` znaczy dwie rzeczy naraz: indeks poza zakresem albo krok
        z placeholderem (alias, merge key). Dla wołającego to ta sama sytuacja —
        lokalizacji nie ma, banner degraduje.
        """

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
        (przycinanie spanów zostawia luki), całej sekcji ``config:`` oraz linii
        kroków z placeholderem — tych po prostu nie znamy.
        """

        for index, loc in enumerate(self.steps):
            if loc is not None and loc.line <= line <= loc.end_line:
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


def _indent_of(line: str) -> int:
    """Liczba znaków wcięcia linii."""

    return len(line) - len(line.lstrip())


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


def _step_locations(document: Any, lines: tuple[str, ...]) -> list[StepLocation | None]:
    """Spany kroków w kolejności ``flat_steps()``; przy wątpliwości placeholder ``None``.

    Lista ma **zawsze** tyle pozycji, ile ``flat_steps()`` — krok, którego nie
    umiemy zlokalizować, zajmuje swoje miejsce jako ``None``, nigdy nie znika.
    """

    if not isinstance(document, CommentedMap):
        return []
    steps = document.get("steps")
    if not isinstance(steps, CommentedSeq):
        return []

    block_end = _steps_block_end(document, lines)
    locations: list[StepLocation | None] = []
    for index, entry in enumerate(steps):
        is_block = isinstance(entry, CommentedMap) and "when" in entry
        start = _item_line(steps, index)
        if start is None:
            locations.extend([None] * _flat_width(entry if is_block else None))
            continue
        next_start = _item_line(steps, index + 1) if index + 1 < len(steps) else None
        end = _trim(lines, start, next_start - 1 if next_start else block_end)

        if is_block:
            locations.extend(_block_locations(entry, lines, start, end))
        else:
            locations.append(StepLocation(line=start, end_line=end, is_gate=False, gate_line=None))
    return _monotonic(locations)


def _flat_width(block: CommentedMap | None) -> int:
    """Ile pozycji ``flat_steps()`` zajmuje wpis: blok to bramka plus dzieci, reszta to 1."""

    if block is None:
        return 1
    children = block.get("steps")
    return 1 + (len(children) if isinstance(children, CommentedSeq) else 0)


def _monotonic(locations: list[StepLocation | None]) -> list[StepLocation | None]:
    """Wymuś spany rozłączne i rosnące — cofający się span zastąp placeholderem.

    Alias listy (``- *consent``) i merge key (``<<: *base``) dostają z ruamela
    pozycję **definicji kotwicy**. Bez tej bramki span cytowałby cudze kroki,
    a nagłówek wskazywał linię, w której danego kroku nie ma.
    """

    checked: list[StepLocation | None] = []
    previous_end = 0
    for loc in locations:
        if loc is None or loc.line <= previous_end:
            checked.append(None)
            continue
        checked.append(loc)
        previous_end = loc.end_line
    return checked


def _block_locations(
    entry: CommentedMap, lines: tuple[str, ...], start: int, end: int
) -> list[StepLocation | None]:
    """Bramka bloku ``when:`` plus jego dzieci — w kolejności wykonania.

    Span bramki kończy się linię przed kluczem ``steps:`` bloku; klucze zapisane
    *po* liście dzieci nie trafią ani do snippetu bramki, ani do snippetu
    ostatniego dziecka — świadome uproszczenie na rzecz ciągłego fragmentu.
    """

    children_key_line = _key_line(entry, "steps")
    gate_end = end if children_key_line is None else _trim(lines, start, children_key_line - 1)
    locations: list[StepLocation | None] = [
        StepLocation(line=start, end_line=gate_end, is_gate=True, gate_line=start)
    ]

    children = entry.get("steps")
    if not isinstance(children, CommentedSeq):
        return locations

    children_end = (
        end if children_key_line is None else _children_end(lines, children_key_line, end)
    )
    for index, _child in enumerate(children):
        child_start = _item_line(children, index)
        if child_start is None:
            locations.append(None)
            continue
        next_start = _item_line(children, index + 1) if index + 1 < len(children) else None
        child_end = _trim(lines, child_start, next_start - 1 if next_start else children_end)
        locations.append(
            StepLocation(line=child_start, end_line=child_end, is_gate=False, gate_line=start)
        )
    return locations


def _children_end(lines: tuple[str, ...], key_line: int, end: int) -> int:
    """Ostatnia linia listy dzieci: przed pierwszym powrotem do wcięcia klucza ``steps:``.

    Bez tego ostatnie dziecko połknęłoby klucze bloku zapisane *po* liście
    (``timeout: 5`` pod ``steps:``), które należą do bloku, nie do kroku.
    """

    if not 1 <= key_line <= len(lines):
        return end
    key_indent = _indent_of(lines[key_line - 1])
    last = key_line
    for number in range(key_line + 1, min(end, len(lines)) + 1):
        text = lines[number - 1]
        if _is_filler(text):
            continue
        if _indent_of(text) <= key_indent:
            break
        last = number
    return last
