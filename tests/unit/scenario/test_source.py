"""Testy mapy źródła YAML (`scenario/source.py`) — spany kroków i linie węzłów."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.env import referenced_env_names
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.scenario.source import ScenarioSource, StepLocation, build_source

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = sorted((REPO_ROOT / "examples").glob("*.scenario.yaml"))


def _line_of(text: str, needle: str) -> int:
    """Numer linii (1-based) pierwszego wystąpienia ``needle`` — oczekiwania bez ręcznego liczenia."""

    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"nie znaleziono {needle!r} w tekście")


def _source(text: str, name: str = "test.scenario.yaml") -> ScenarioSource:
    return build_source(Path(name), text)


CONFIG = """\
config:
  title: "t"
  viewport:
    width: 800
    height: 600
  tts: { provider: edge, voice: v, lang: en-US }
"""

SIMPLE = CONFIG + textwrap.dedent(
    """\
    steps:
      - say: "pierwszy"

      # komentarz między krokami
      - navigate: "https://example.test"
      - when: "baner zgody"
        state: visible
        timeout: 5
        steps:
          - click: "ok"

          - say: "po kliknięciu"
      - say: "ostatni"
    """
)

TRAILING_KEY = CONFIG + textwrap.dedent(
    """\
    steps:
      - say: "jedyny"

    # ogon
    extra: 1
    """
)

LONG_STEP = CONFIG + textwrap.dedent(
    """\
    steps:
      - slide:
          title: "tytuł"
          subtitle: "podtytuł"
          notes: |
            linia 1
            linia 2
            linia 3
            linia 4
            linia 5
          hold: 3
      - say: "po slajdzie"
    """
)

BROKEN_STEPS = CONFIG + textwrap.dedent(
    """\
    steps:
      - enterText:
          into: "pole"
          text: 5
      - when: "baner"
        state: visible
        steps:
          - nope: 1
    """
)


# --- spany kroków -----


def test_top_level_step_span_is_its_own_line():
    src = _source(SIMPLE)

    assert src.location(0) == StepLocation(
        line=_line_of(SIMPLE, '- say: "pierwszy"'),
        end_line=_line_of(SIMPLE, '- say: "pierwszy"'),
        is_gate=False,
        gate_line=None,
    )


def test_span_is_trimmed_of_trailing_blank_and_comment_lines():
    src = _source(SIMPLE)
    first = src.location(0)

    # następne rodzeństwo jest dopiero 3 linie dalej — pusta linia i komentarz
    # nie należą do kroku
    assert first.end_line == first.line
    assert src.location(1).line == _line_of(SIMPLE, '- navigate: "https://example.test"')


def test_gate_span_covers_block_head_without_children():
    src = _source(SIMPLE)
    gate = src.location(2)

    assert gate.is_gate is True
    assert gate.line == _line_of(SIMPLE, '- when: "baner zgody"')
    assert gate.end_line == _line_of(SIMPLE, "timeout: 5")
    assert gate.gate_line == gate.line


def test_children_of_block_follow_the_gate():
    src = _source(SIMPLE)
    gate_line = _line_of(SIMPLE, '- when: "baner zgody"')

    first_child = src.location(3)
    second_child = src.location(4)

    assert first_child == StepLocation(
        line=_line_of(SIMPLE, '- click: "ok"'),
        end_line=_line_of(SIMPLE, '- click: "ok"'),
        is_gate=False,
        gate_line=gate_line,
    )
    assert second_child == StepLocation(
        line=_line_of(SIMPLE, '- say: "po kliknięciu"'),
        end_line=_line_of(SIMPLE, '- say: "po kliknięciu"'),
        is_gate=False,
        gate_line=gate_line,
    )


def test_step_after_block_is_top_level_again():
    src = _source(SIMPLE)
    last = src.location(5)

    assert last.gate_line is None
    assert last.is_gate is False
    assert last.line == _line_of(SIMPLE, '- say: "ostatni"')
    assert src.location(6) is None


def test_last_step_span_reaches_the_end_of_file():
    src = _source(LONG_STEP)
    last = src.location(1)

    assert last.line == _line_of(LONG_STEP, '- say: "po slajdzie"')
    assert last.end_line == len(LONG_STEP.splitlines())


def test_last_step_span_stops_before_the_next_top_level_key():
    src = _source(TRAILING_KEY)
    only = src.location(0)

    assert only.line == _line_of(TRAILING_KEY, '- say: "jedyny"')
    assert only.end_line == only.line  # `# ogon` i pusta linia przycięte


# --- luki i index_at_line -----


def test_index_at_line_maps_every_line_of_a_span():
    src = _source(SIMPLE)
    gate = src.location(2)

    for line in range(gate.line, gate.end_line + 1):
        assert src.index_at_line(line) == 2


def test_index_at_line_is_none_in_gaps_between_steps():
    src = _source(SIMPLE)

    assert src.index_at_line(_line_of(SIMPLE, "# komentarz między krokami")) is None
    assert src.index_at_line(_line_of(SIMPLE, "# komentarz między krokami") - 1) is None


def test_index_at_line_is_none_inside_config():
    src = _source(SIMPLE)

    assert src.index_at_line(_line_of(SIMPLE, "config:")) is None
    assert src.index_at_line(_line_of(SIMPLE, "width: 800")) is None


def test_index_at_line_is_none_for_the_steps_key_itself():
    src = _source(SIMPLE)

    assert src.index_at_line(_line_of(SIMPLE, "steps:")) is None


def test_measured_spans_of_the_onet_example():
    path = REPO_ROOT / "examples" / "onet-login.scenario.yaml"
    src = build_source(path, path.read_text(encoding="utf-8"))

    assert [(loc.line, loc.end_line) for loc in src.steps] == [
        (30, 30),
        (33, 33),
        (37, 39),
        (41, 41),
        (43, 43),
        (45, 45),
        (47, 47),
        (49, 49),
    ]
    assert [loc.is_gate for loc in src.steps] == [
        False,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert [loc.gate_line for loc in src.steps] == [None, None, 37, 37, None, None, None, None]


# --- totalność: build_source dostaje też pliki niepoprawne -----


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("steps skalarem", "config: {}\nsteps: hello\n"),
        ("wpis listy skalarem", "config: {}\nsteps:\n  - hello\n  - say: x\n"),
        ("blok when bez steps", 'config: {}\nsteps:\n  - when: "baner"\n    state: visible\n'),
        ("steps bloku niebędące listą", 'config: {}\nsteps:\n  - when: "b"\n    steps: nope\n'),
        ("brak steps", "config: {}\n"),
        ("pusty plik", ""),
        ("dokument skalarny", "hello\n"),
        ("plik składniowo zepsuty", "steps:\n  - [unclosed\n"),
    ],
)
def test_build_source_never_raises(name, text):
    src = _source(text)

    assert isinstance(src, ScenarioSource)
    assert isinstance(src.steps, tuple)
    assert src.lines == tuple(text.splitlines())


def test_broken_syntax_degrades_to_empty_steps_with_lines_intact():
    text = "config: {}\nsteps:\n  - [unclosed\n"
    src = _source(text)

    assert src.steps == ()
    assert src.lines == ("config: {}", "steps:", "  - [unclosed")
    assert src.node_line(("steps", 0)) is None


def test_block_without_steps_still_yields_the_gate():
    text = 'config: {}\nsteps:\n  - when: "baner"\n    state: visible\n'
    src = _source(text)

    assert len(src.steps) == 1
    assert src.steps[0].is_gate is True


def test_scalar_steps_yields_no_spans():
    src = _source("config: {}\nsteps: hello\n")

    assert src.steps == ()


# --- node_line -----


def _validation_locs(text: str) -> list[tuple]:
    """Prawdziwe ścieżki `loc` pydantica dla zepsutego scenariusza (nie zgadywane)."""

    raw = YAML(typ="safe").load(text)
    with pytest.raises(ValidationError) as excinfo:
        Scenario.model_validate(raw)
    return [error["loc"] for error in excinfo.value.errors()]


def test_node_line_skips_the_union_variant_tag():
    src = _source(BROKEN_STEPS)
    locs = _validation_locs(BROKEN_STEPS)
    loc = next(loc for loc in locs if loc[:2] == ("steps", 0) and loc[-1] == "text")

    # tag wariantu unii nie jest kluczem mapy — musi zostać pominięty, nie zatrzymać marszu
    assert isinstance(loc[2], str) and "Step" in loc[2]
    assert src.node_line(loc) == _line_of(BROKEN_STEPS, "text: 5")


def test_node_line_of_a_block_child_points_at_the_child_not_the_block():
    src = _source(BROKEN_STEPS)
    locs = _validation_locs(BROKEN_STEPS)
    loc = next(loc for loc in locs if loc[:2] == ("steps", 1) and loc[-1] == "nope")

    assert loc[2] == "WhenBlock"
    assert src.node_line(loc) == _line_of(BROKEN_STEPS, "- nope: 1")
    assert src.node_line(("steps", 1, "WhenBlock", "steps", 0)) == _line_of(
        BROKEN_STEPS, "- nope: 1"
    )
    assert src.node_line(("steps", 1, "WhenBlock", "steps", 0)) != _line_of(
        BROKEN_STEPS, '- when: "baner"'
    )


def test_node_line_of_a_config_field():
    src = _source(BROKEN_STEPS)

    assert src.node_line(("config", "viewport")) == _line_of(BROKEN_STEPS, "viewport:")
    assert src.node_line(("config", "viewport", "width")) == _line_of(BROKEN_STEPS, "width: 800")


def test_node_line_of_an_empty_path_is_none():
    assert _source(BROKEN_STEPS).node_line(()) is None


def test_node_line_of_a_fully_unaddressable_path_is_none():
    assert _source(BROKEN_STEPS).node_line(("nie-ma-takiego", 7)) is None


def test_node_line_returns_the_deepest_addressable_node():
    src = _source(BROKEN_STEPS)

    # indeks poza listą jest nieadresowalny — zostaje linia klucza `steps:`
    assert src.node_line(("steps", 99)) == _line_of(BROKEN_STEPS, "steps:")


# --- snippet / line_snippet -----


def test_snippet_returns_the_full_span_without_truncation():
    src = _source(LONG_STEP)
    loc = src.location(0)
    snippet = src.snippet(loc)

    assert len(snippet) == loc.end_line - loc.line + 1
    assert len(snippet) > 8  # ucięcie należy do render_banner, nie do snippet
    assert snippet[0] == (loc.line, "  - slide:")
    assert snippet[-1][0] == loc.end_line


def test_snippet_keeps_the_literal_line_text():
    src = _source(SIMPLE)
    snippet = src.snippet(src.location(2))

    assert snippet == [
        (_line_of(SIMPLE, '- when: "baner zgody"'), '  - when: "baner zgody"'),
        (_line_of(SIMPLE, "state: visible"), "    state: visible"),
        (_line_of(SIMPLE, "timeout: 5"), "    timeout: 5"),
    ]


def test_line_snippet_returns_a_single_line():
    src = _source(SIMPLE)
    line = _line_of(SIMPLE, "# komentarz między krokami")

    assert src.line_snippet(line) == [(line, "  # komentarz między krokami")]


def test_line_snippet_out_of_range_is_empty():
    src = _source(SIMPLE)

    assert src.line_snippet(0) == []
    assert src.line_snippet(len(SIMPLE.splitlines()) + 5) == []


def test_location_out_of_range_is_none():
    src = _source(SIMPLE)

    assert src.location(-1) is None
    assert src.location(999) is None


# --- cache -----


def test_build_source_is_cached_by_path_and_text():
    first = build_source(Path("a.scenario.yaml"), SIMPLE)
    again = build_source(Path("a.scenario.yaml"), SIMPLE)
    other_path = build_source(Path("b.scenario.yaml"), SIMPLE)

    assert first is again
    assert other_path is not first
    assert other_path.path == Path("b.scenario.yaml")


# --- dopięcie do load_scenario -----


def test_load_scenario_attaches_the_source(tmp_path: Path):
    path = tmp_path / "x.scenario.yaml"
    path.write_text(SIMPLE, encoding="utf-8")

    scenario = load_scenario(path, env={})

    assert scenario.source is not None
    assert scenario.source.path == path
    assert [entry.location for entry in scenario.flat_steps()] == list(scenario.source.steps)


def test_attached_source_holds_text_from_before_env_substitution(tmp_path: Path):
    text = CONFIG + textwrap.dedent(
        """\
        steps:
          - enterText:
              into: "hasło"
              text: "${SECRET}"
        """
    )
    path = tmp_path / "secret.scenario.yaml"
    path.write_text(text, encoding="utf-8")

    scenario = load_scenario(path, env={"SECRET": "hunter2"})
    rendered = "\n".join(line for _, line in scenario.source.snippet(scenario.source.steps[0]))

    assert "hunter2" not in rendered
    assert "${SECRET}" in rendered


# --- regresja spójności z flat_steps() -----


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_spans_line_up_with_flat_steps(example: Path):
    text = example.read_text(encoding="utf-8")
    env = {name: "x" for name in referenced_env_names(YAML(typ="safe").load(text))}

    src = build_source(example, text)
    flat = load_scenario(example, env=env).flat_steps()

    assert len(src.steps) == len(flat)
    assert [loc.is_gate for loc in src.steps] == [entry.is_gate for entry in flat]


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_spans_are_increasing_and_disjoint(example: Path):
    text = example.read_text(encoding="utf-8")
    src = build_source(example, text)
    total = len(text.splitlines())

    previous_end = 0
    for loc in src.steps:
        assert 1 <= loc.line <= loc.end_line <= total
        assert loc.line > previous_end  # rozłączne i rosnące
        previous_end = loc.end_line
