"""Testy formatera bannerów diagnostycznych (`guidebot_recorder.diagnostics`).

Atrapy `FakeLocation` / `FakeSource` odtwarzają kontrakt `StepLocation`
i `ScenarioSource` ze specyfikacji, żeby te testy przechodziły niezależnie od
modułu `guidebot_recorder.scenario.source`.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from guidebot_recorder.diagnostics import render_banner, step_banner, validation_banner


@dataclass(frozen=True)
class FakeLocation:
    """Atrapa `StepLocation` (linie 1-based, obustronnie domknięte)."""

    line: int
    end_line: int
    is_gate: bool = False
    gate_line: int | None = None


@dataclass(frozen=True)
class FakeSource:
    """Atrapa `ScenarioSource` — tylko to, czego używa formatter."""

    path: Path
    lines: tuple[str, ...]
    steps: tuple[FakeLocation, ...] = field(default=())

    def location(self, index: int) -> FakeLocation | None:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def snippet(self, loc: FakeLocation) -> list[tuple[int, str]]:
        return [(nr, self.lines[nr - 1]) for nr in range(loc.line, loc.end_line + 1)]

    def line_snippet(self, line: int) -> list[tuple[int, str]]:
        return [(line, self.lines[line - 1])]


def expected(text: str) -> str:
    """Blok tekstu w teście → dokładny oczekiwany banner (bez skrajnych pustych linii)."""

    return textwrap.dedent(text).strip("\n")


def _numbered_source(path: str, first_line: int, contents: list[str]) -> FakeSource:
    """Źródło, w którym `contents` zaczyna się w linii `first_line` (wcześniej puste linie)."""

    lines = [""] * (first_line - 1) + contents
    return FakeSource(path=Path(path), lines=tuple(lines))


# --- onet-login: wzorce wprost ze specyfikacji ------------------------------

ONET = _numbered_source(
    "examples/onet-login.scenario.yaml",
    37,
    [
        '  - when: "the cookie consent banner"',
        "    state: visible",
        "    timeout: 20",
        "    steps:",
        '      - teach: "First we accept the cookie consent by clicking..."',
    ],
)


def test_step_banner_bramki_odtwarza_wzorzec_ze_specyfikacji():
    banner = step_banner(
        index=2,
        total=8,
        location=FakeLocation(line=37, end_line=39, is_gate=True, gate_line=None),
        source=ONET,
        message=(
            "element bramkujący nie pojawił się — zapisano wpis oczekujący (pending);\n"
            "render rozwiąże go na miejscu"
        ),
        warning=True,
    )
    assert banner == expected(
        """
        ⚠ krok 3/8 — examples/onet-login.scenario.yaml:37 (bramka `when:`)
             37 |   - when: "the cookie consent banner"
             38 |     state: visible
             39 |     timeout: 20
           element bramkujący nie pojawił się — zapisano wpis oczekujący (pending);
           render rozwiąże go na miejscu
        """
    )


def test_step_banner_dziecka_bramki_odtwarza_wzorzec_ze_specyfikacji():
    banner = step_banner(
        index=3,
        total=8,
        location=FakeLocation(line=41, end_line=41, is_gate=False, gate_line=37),
        source=ONET,
        message="element opcjonalny nie pojawił się",
        warning=True,
    )
    assert banner == expected(
        """
        ⚠ krok 4/8 — examples/onet-login.scenario.yaml:41 (w bramce z linii 37)
             41 |       - teach: "First we accept the cookie consent by clicking..."
           element opcjonalny nie pojawił się
        """
    )


def test_step_banner_top_level_bez_sufiksu_i_bez_prefiksu():
    source = _numbered_source("scen.yaml", 10, ['  - click: "Zapisz"'])
    banner = step_banner(
        index=0,
        total=3,
        location=FakeLocation(line=10, end_line=10, is_gate=False, gate_line=None),
        source=source,
        message="element nie znaleziony",
    )
    assert banner == expected(
        """
        krok 1/3 — scen.yaml:10
             10 |   - click: "Zapisz"
           element nie znaleziony
        """
    )


def test_step_banner_wypisuje_sciezke_doslownie():
    source = _numbered_source("../examples/x.scenario.yaml", 1, ["  - teach: cześć"])
    banner = step_banner(
        index=0,
        total=1,
        location=FakeLocation(line=1, end_line=1),
        source=source,
        message="treść",
    )
    assert banner.splitlines()[0] == "krok 1/1 — ../examples/x.scenario.yaml:1"


# --- degradacja: brak lokalizacji / brak źródła -----------------------------


def test_step_banner_bez_lokalizacji_daje_sam_naglowek_i_tresc():
    banner = step_banner(
        index=3,
        total=8,
        location=None,
        source=ONET,
        message="element opcjonalny nie pojawił się",
    )
    assert banner == expected(
        """
        krok 4/8
           element opcjonalny nie pojawił się
        """
    )


def test_step_banner_bez_zrodla_degraduje_mimo_lokalizacji():
    banner = step_banner(
        index=0,
        total=2,
        location=FakeLocation(line=5, end_line=6),
        source=None,
        message="treść",
        warning=True,
    )
    assert banner == expected(
        """
        ⚠ krok 1/2
           treść
        """
    )


def test_step_banner_z_pustym_snippetem_nie_dokleja_pustej_linii():
    source = FakeSource(path=Path("pusty.yaml"), lines=())
    banner = step_banner(
        index=0,
        total=1,
        location=FakeLocation(line=1, end_line=0),
        source=source,
        message="treść",
    )
    assert banner == expected(
        """
        krok 1/1 — pusty.yaml:1
           treść
        """
    )


# --- render_banner: format --------------------------------------------------


def test_render_banner_ucina_snippet_po_osmiu_liniach():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 13)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat")
    assert banner == expected(
        """
        NAGŁÓWEK
              1 | linia 1
              2 | linia 2
              3 | linia 3
              4 | linia 4
              5 | linia 5
              6 | linia 6
              7 | linia 7
              8 | linia 8
                … (jeszcze 4 linie)
           komunikat
        """
    )


def test_render_banner_nie_ucina_dokladnie_osmiu_linii():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 9)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat")
    assert "…" not in banner
    assert banner.splitlines()[-2] == "      8 | linia 8"


def test_render_banner_respektuje_wlasny_max_lines():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 5)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", max_lines=2)
    assert banner == expected(
        """
        NAGŁÓWEK
              1 | linia 1
              2 | linia 2
                … (jeszcze 2 linie)
           komunikat
        """
    )


@pytest.mark.parametrize(
    ("hidden", "form"),
    [
        (1, "1 linia"),
        (2, "2 linie"),
        (3, "3 linie"),
        (4, "4 linie"),
        (5, "5 linii"),
        (11, "11 linii"),
        (12, "12 linii"),
        (13, "13 linii"),
        (14, "14 linii"),
        (21, "21 linii"),
        (22, "22 linie"),
        (23, "23 linie"),
        (24, "24 linie"),
        (25, "25 linii"),
        (112, "112 linii"),
        (113, "113 linii"),
        (122, "122 linie"),
    ],
)
def test_render_banner_odmienia_liczbe_ukrytych_linii(hidden: int, form: str):
    snippet = [(nr, f"linia {nr}") for nr in range(1, 1 + hidden)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", max_lines=0)
    assert banner.splitlines()[1] == f"        … (jeszcze {form})"


def test_render_banner_stawia_karetke_pod_wlasciwa_linia_nie_na_koncu():
    snippet = [(23, '  - click: "Zapisz"'), (24, '    navigate: "https://example.test"')]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=23)
    assert banner == expected(
        """
        NAGŁÓWEK
             23 |   - click: "Zapisz"
                  ^ tutaj
             24 |     navigate: "https://example.test"
           komunikat
        """
    )


def test_render_banner_karetka_pod_ostatnia_linia_snippetu():
    snippet = [(23, "a"), (24, "b")]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=24)
    assert banner == expected(
        """
        NAGŁÓWEK
             23 | a
             24 | b
                  ^ tutaj
           komunikat
        """
    )


def test_render_banner_pomija_karetke_dla_linii_spoza_snippetu():
    snippet = [(23, "a"), (24, "b")]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=99)
    assert "^ tutaj" not in banner


def test_render_banner_przesuwa_okno_gdy_karetka_wypada_za_uciecie():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 13)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=11)
    assert banner == expected(
        """
        NAGŁÓWEK
                … (wcześniej 4 linie)
              5 | linia 5
              6 | linia 6
              7 | linia 7
              8 | linia 8
              9 | linia 9
             10 | linia 10
             11 | linia 11
                  ^ tutaj
             12 | linia 12
           komunikat
        """
    )


def test_render_banner_centruje_okno_na_karetce_i_ucina_z_obu_stron():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 21)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=12)
    assert banner == expected(
        """
        NAGŁÓWEK
                … (wcześniej 8 linii)
              9 | linia 9
             10 | linia 10
             11 | linia 11
             12 | linia 12
                  ^ tutaj
             13 | linia 13
             14 | linia 14
             15 | linia 15
             16 | linia 16
                … (jeszcze 4 linie)
           komunikat
        """
    )


def test_render_banner_nie_rusza_okna_gdy_karetka_miesci_sie_w_uciecu():
    snippet = [(nr, f"linia {nr}") for nr in range(1, 13)]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat", caret_line=8)
    assert banner.splitlines()[1] == "      1 | linia 1"
    assert "wcześniej" not in banner
    assert banner.splitlines()[-2] == "        … (jeszcze 4 linie)"


def test_render_banner_zachowuje_doslowna_tresc_linii():
    snippet = [(7, "\t  - teach: 'x'   ")]
    banner = render_banner("NAGŁÓWEK", snippet, "komunikat")
    assert banner.splitlines()[1] == "      7 | \t  - teach: 'x'   "


def test_render_banner_wcina_kazda_linie_komunikatu():
    banner = render_banner("NAGŁÓWEK", [], "pierwsza\ndruga\n\nczwarta")
    assert banner == expected(
        """
        NAGŁÓWEK
           pierwsza
           druga

           czwarta
        """
    )


def test_render_banner_bez_snippetu_i_bez_komunikatu():
    assert render_banner("NAGŁÓWEK", [], "") == "NAGŁÓWEK"


# --- validation_banner ------------------------------------------------------

BPP = _numbered_source(
    "examples/bpp.scenario.yaml",
    23,
    ['  - click: "Zapisz"', '    navigate: "https://example.test"'],
)
BPP_ZE_SPANEM = FakeSource(
    path=BPP.path,
    lines=BPP.lines,
    steps=(FakeLocation(line=23, end_line=24),) * 5,
)


def test_validation_banner_odtwarza_wzorzec_ze_specyfikacji():
    banner = validation_banner(
        source=BPP_ZE_SPANEM,
        line=23,
        index=4,
        total=12,
        message="krok ma 2 komend (['navigate', 'click']); dozwolona dokładnie jedna",
    )
    assert banner == expected(
        """
        BŁĄD walidacji — examples/bpp.scenario.yaml:23 (krok 5/12)
             23 |   - click: "Zapisz"
                  ^ tutaj
             24 |     navigate: "https://example.test"
           krok ma 2 komend (['navigate', 'click']); dozwolona dokładnie jedna
        """
    )


def test_validation_banner_bez_indeksu_pokazuje_sama_linie():
    banner = validation_banner(
        source=BPP_ZE_SPANEM,
        line=24,
        index=None,
        total=12,
        message="nieznane pole",
    )
    assert banner == expected(
        """
        BŁĄD walidacji — examples/bpp.scenario.yaml:24
             24 |     navigate: "https://example.test"
                  ^ tutaj
           nieznane pole
        """
    )


def test_validation_banner_bez_linii_daje_sama_sciezke():
    banner = validation_banner(
        source=BPP_ZE_SPANEM,
        line=None,
        index=None,
        total=12,
        message="scenariusz musi mieć co najmniej jeden krok",
    )
    assert banner == expected(
        """
        BŁĄD walidacji — examples/bpp.scenario.yaml
           scenariusz musi mieć co najmniej jeden krok
        """
    )


def test_validation_banner_bez_zrodla_daje_sam_naglowek():
    banner = validation_banner(
        source=None,
        line=23,
        index=4,
        total=12,
        message="treść",
    )
    assert banner == expected(
        """
        BŁĄD walidacji
           treść
        """
    )


def test_validation_banner_dla_indeksu_bez_lokalizacji_spada_do_jednej_linii():
    source = FakeSource(path=BPP.path, lines=BPP.lines, steps=())
    banner = validation_banner(
        source=source,
        line=23,
        index=4,
        total=12,
        message="treść",
    )
    assert banner == expected(
        """
        BŁĄD walidacji — examples/bpp.scenario.yaml:23 (krok 5/12)
             23 |   - click: "Zapisz"
                  ^ tutaj
           treść
        """
    )


# --- redakcja sekretów ------------------------------------------------------


def test_step_banner_redaguje_sekret_w_tresci_komunikatu():
    source = _numbered_source("scen.yaml", 4, ['    text: "${SECRET}"'])
    banner = step_banner(
        index=0,
        total=1,
        location=FakeLocation(line=4, end_line=4),
        source=source,
        message="element 'hunter2' nie pojawił się",
        warning=True,
        sensitive=("hunter2",),
    )
    assert "hunter2" not in banner
    assert banner == expected(
        """
        ⚠ krok 1/1 — scen.yaml:4
              4 |     text: "${SECRET}"
           element '<redacted>' nie pojawił się
        """
    )


def test_step_banner_redaguje_sekret_takze_w_snippecie():
    source = _numbered_source("scen.yaml", 1, ['    text: "hunter2"'])
    banner = step_banner(
        index=0,
        total=1,
        location=FakeLocation(line=1, end_line=1),
        source=source,
        message="treść",
        sensitive=["hunter2"],
    )
    assert "hunter2" not in banner
    assert '"<redacted>"' in banner


def test_step_banner_redaguje_forme_url_encoded():
    banner = step_banner(
        index=0,
        total=1,
        location=None,
        source=None,
        message="URL: https://x.test/?p=tajne%20has%C5%82o",
        sensitive=("tajne hasło",),
    )
    assert banner == expected(
        """
        krok 1/1
           URL: https://x.test/?p=<redacted>
        """
    )


def test_step_banner_bez_sensitive_nie_rusza_tresci():
    banner = step_banner(
        index=0,
        total=1,
        location=None,
        source=None,
        message="hunter2",
    )
    assert banner == "krok 1/1\n   hunter2"
