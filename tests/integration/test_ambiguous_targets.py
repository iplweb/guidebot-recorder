"""E2E: niejednoznaczny namiar na formularzu o powtarzalnych wierszach (#51).

Fixture (``fixtures/criteria-rows.html``) odtwarza formularz kryteriów
django-multiseek ze zgłoszenia: wiersze z nienazwanym krzyżykiem (ikona
``aria-hidden``, bo ``<button>×</button>`` **ma** nazwę dostępną „×") oraz
wiersz „Zakres lat" z dwoma nienazwanymi polami tekstowymi. To ta druga rzecz
jest w zgłoszeniu najbardziej wymowna: dwa różne kroki dostały identyczny target
``role=textbox name='' nth=1``, więc obie wartości trafiły do jednego pola.

Sercem tego pliku jest **para** atrap reasonera, nie pojedyncza atrapa:

``GuessingReasoner``
    model sprzed zmiany — zgaduje ``nth``, licząc pozycję w tablicy JSON, którą
    dostał w prompcie (czyli w snapshocie :func:`collect_candidates`). Atrapa
    jest zwykłym obiektem implementującym protokół ``Reasoner``, więc może
    zwrócić ``RoleTarget(nth=…)`` mimo że ``CodexReasoner`` już nie może —
    i o to chodzi: bez tej połowy test mierzyłby wyłącznie arytmetykę
    ``pin_position`` i świeciłby na zielono, niczego nie dowodząc.

``NamingReasoner``
    model po zmianie — wskazuje ``candidateId``, a indeks mierzy maszyna.

Obie atrapy identyfikują ten sam element tą samą, geometryczną regułą (lewe/
prawe pole w wierszu z parą nienazwanych pól), więc jedyną różnicą między
przebiegami jest protokół odpowiedzi. Rozjazd, który zgubił model, jest wprost
w liczbach: snapshot ma **pięć** pól tekstowych (z nazwanym „Szukana fraza"),
a lokator ``get_by_role("textbox", name="")`` trafia w **cztery** nienazwane.

Reasoner zamockowany (deterministyczny), TTS fałszywy (ciche mp3) — bez sieci
i bez Codexa.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import compile_up_to_date, run_compile
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.positional import pinned_drifted
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.validate import build_locator, reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.video.mux import probe_duration

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "criteria-rows.html"

#: Miejsce w fixture, w które test dryfu wstrzykuje dodatkowe pole.
_DRIFT_SLOT = "<!--GNIAZDO-DRYFU-->"

#: Nienazwane pole tekstowe doklejone do wiersza „Szukana fraza". Patrz
#: :func:`test_drift_reresolves_through_the_gate_the_cli_uses` — miejsce
#: wstrzyknięcia nie jest dowolne.
_EXTRA_FIELD = '<input type="text" id="fraza-dodatkowa" />'


# --- scenariusze ------------------------------------------------------------

YEARS_SCENARIO = """\
config:
  title: Zakres lat
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - navigate: "{url}"
  - enterText: {{into: "lewe pole roku (od)", text: "2022"}}
  - enterText: {{into: "prawe pole roku (do)", text: "2024"}}
"""

FULL_CYCLE_SCENARIO = """\
config:
  title: Kryteria wyszukiwania
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Zawężam wyniki do lat 2022-2024."
  - navigate: "{url}"
  - enterText: {{into: "lewe pole roku (od)", text: "2022"}}
  - enterText: {{into: "prawe pole roku (do)", text: "2024"}}
  - click: "krzyżyk przy ostatnim wierszu kryterium"
"""

UNIQUE_SCENARIO = """\
config:
  title: Jednoznaczny namiar
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - navigate: "{url}"
  - click: "przycisk Szukaj"
"""


# --- atrapy reasonera -------------------------------------------------------


def _unnamed(candidates: list[Candidate], role: str) -> list[Candidate]:
    return [c for c in candidates if c.role == role and c.name == ""]


def _year_fields(candidates: list[Candidate]) -> tuple[Candidate, Candidate]:
    """Para nienazwanych pól tekstowych leżąca w jednym wierszu — „Zakres lat".

    Reguła jest geometryczna, bo taka jest też instrukcja w scenariuszu („lewe"
    / „prawe"). Obie atrapy używają jej wspólnie, więc porównanie przebiegów
    mierzy różnicę protokołu odpowiedzi, a nie różnicę „inteligencji" atrap.
    """

    rows: dict[int, list[Candidate]] = {}
    for candidate in _unnamed(candidates, "textbox"):
        rows.setdefault(round(candidate.bbox[1]), []).append(candidate)
    pair = next(row for row in rows.values() if len(row) == 2)
    left, right = sorted(pair, key=lambda candidate: candidate.bbox[0])
    return left, right


def _wanted(instruction: str, candidates: list[Candidate]) -> tuple[str, Candidate]:
    """Akcja i kandydat, o którym mówi instrukcja — wspólne dla obu atrap."""

    if "krzyżyk" in instruction:
        buttons = _unnamed(candidates, "button")
        # „ostatni wiersz kryterium" — czyli najniżej położony krzyżyk.
        return "click", max(buttons, key=lambda candidate: candidate.bbox[1])
    if "Szukaj" in instruction:
        return "click", next(c for c in candidates if c.name == "Szukaj")
    left, right = _year_fields(candidates)
    if "(od)" in instruction:
        return "type", left
    if "(do)" in instruction:
        return "type", right
    raise AssertionError(f"nieoczekiwana instrukcja: {instruction!r}")


class GuessingReasoner:
    """Model sprzed zmiany: indeks liczony na tablicy kandydatów z promptu.

    Spec nazywa to przyczyną źródłową #51 — model **nie ma z czego** wyliczyć
    ``nth``. Snapshot nie zawiera żadnego indeksu, więc jedyne, co da się zrobić,
    to policzyć pozycję w tablicy JSON, którą widać w prompcie. Playwright liczy
    tymczasem trafienia *swojego* lokatora ``get_by_role(role, name=…)``. To dwa
    różne zbiory i ta atrapa robi dokładnie ten błąd — bez żadnej złośliwości,
    po prostu licząc to, co ma przed oczami.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        action, wanted = _wanted(instruction, candidates)
        same_role = [c for c in candidates if c.role == wanted.role]
        guess = next(i for i, c in enumerate(same_role) if c is wanted)
        return ReasonerResult(
            action,
            RoleTarget(role=wanted.role, name=wanted.name, nth=guess),
        )


class NamingReasoner:
    """Model po zmianie: mówi *który* element ma na myśli, indeks liczy maszyna."""

    def __init__(self) -> None:
        self.calls = 0
        self.feedback: list[str] = []

    async def resolve(self, instruction, candidates, feedback=None):
        self.calls += 1
        if feedback:
            self.feedback.append(feedback)
        action, wanted = _wanted(instruction, candidates)
        return ReasonerResult(
            action,
            RoleTarget(role=wanted.role, name=wanted.name),
            candidate_id=wanted.id,
        )


class FakeTts:
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "0.3",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return 0.3


# --- pomocnicze -------------------------------------------------------------


def _install_page(tmp_path: Path, *, extra_field: bool = False) -> Path:
    """Zapisz fixture do ``tmp_path`` (adres stały, treść wymienialna)."""

    html = FIXTURE.read_text(encoding="utf-8")
    html = html.replace(_DRIFT_SLOT, _EXTRA_FIELD if extra_field else "")
    page_path = tmp_path / "kryteria.html"
    page_path.write_text(html, encoding="utf-8")
    return page_path


def _write_scenario(tmp_path: Path, name: str, template: str, page_path: Path) -> Path:
    path = tmp_path / name
    path.write_text(template.format(url=page_path.resolve().as_uri()), encoding="utf-8")
    return path


async def _values(page) -> dict[str, str]:
    """Wartości wszystkich pól tekstowych formularza, po ``id``."""

    return await page.evaluate(
        "() => Object.fromEntries("
        "Array.from(document.querySelectorAll('#kryteria input[type=text]'),"
        " (el) => [el.id, el.value]))"
    )


async def _rows(page) -> list[str]:
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll('#kryteria .row > span,"
        " #kryteria .row > label'), (el) => el.textContent.trim())"
    )


def _targets(path: Path) -> list[RoleTarget | None]:
    compiled = load_compiled(compiled_path(path))
    return [None if action is None else action.target for action in compiled.actions]


async def _compile_on_page(browser, path: Path, reasoner, **kwargs):
    """Jeden przebieg `compile` na własnym kontekście — zwraca żywą stronę.

    Kontekst musi przeżyć wywołanie, bo asercje czytają wartości pól wpisane
    *przez samą kompilację*; ``run_compile_in_browser`` zamyka go w ``finally``.
    Zamknięcie należy do wołającego.
    """

    context = await browser.new_context(viewport={"width": 800, "height": 600})
    page = await context.new_page()
    await run_compile(path, page, reasoner, selects=None, **kwargs)
    return page


# --- dowód negatywny --------------------------------------------------------


async def test_guessed_index_lands_on_the_wrong_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pierwsza połowa pary: model zgadujący ``nth`` trafia w cudze pola.

    To jest awaria ze zgłoszenia odtworzona w całości — z zieloną kompilacją
    włącznie. Nic nie protestuje: ``validate_compile_time`` widzi jedno
    trafienie (bo ``nth`` już zawęził lokator do jednego elementu), tożsamość
    zgadza się co do ``tag`` i ``ancestry_digest`` (wszystkie wiersze mają
    identyczną strukturę przodków), a wartości spokojnie lądują nie tam, gdzie
    trzeba. Błąd wychodzi dopiero, gdy człowiek obejrzy film.

    Arytmetyka jest dosłownie ta ze specu: snapshot ma pięć pól tekstowych
    (`Szukana fraza`, `rok-od`, `rok-do`, `tytul-wartosc`, `autor-wartosc`),
    a lokator ``name=''`` trafia w cztery nienazwane. Indeks jest więc o jeden
    za duży i każdy krok przesuwa się o jedno pole w prawo.
    """

    page_path = _install_page(tmp_path)
    path = _write_scenario(tmp_path, "lata.scenario.yaml", YEARS_SCENARIO, page_path)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await _compile_on_page(browser, path, GuessingReasoner())
        values = await _values(page)
        await page.context.close()
        await browser.close()

    # Kompilacja przeszła bez błędu i zamroziła dwa indeksy — oba zgadnięte.
    _, od, do = _targets(path)
    assert (od.role, od.name, od.nth) == ("textbox", "", 1)
    assert (do.role, do.name, do.nth) == ("textbox", "", 2)

    # ...i oba wskazują cudzy element. „Od" wpisało się do pola „do", a „do"
    # nadpisało wartość w zupełnie innym wierszu kryterium — dokładnie ten
    # skutek opisuje tabela dowodowa w zgłoszeniu.
    assert values["rok-od"] == ""
    assert values["rok-do"] == "2022"
    assert values["tytul-wartosc"] == "2024"

    # I ani jednego słowa ostrzeżenia: indeks przyszedł gotowy od „modelu",
    # więc `pin_position` nigdy go nie mierzyło i baner nie ma się z czego
    # wziąć. Tak właśnie wyglądało pięć błędnych zamrożeń ze zgłoszenia.
    assert "namiar pozycyjny" not in capsys.readouterr().out


async def test_named_candidate_lands_on_the_fields_the_scenario_describes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Druga połowa pary: ta sama strona, ten sam wybór elementu, inny protokół.

    Atrapa wskazuje ``candidateId`` i nie zwraca ``nth`` wcale; indeks mierzy
    ``pin_position``. Różnica względem testu wyżej jest wyłącznie w protokole
    odpowiedzi — reguła wyboru elementu (:func:`_year_fields`) jest wspólna.
    """

    page_path = _install_page(tmp_path)
    path = _write_scenario(tmp_path, "lata.scenario.yaml", YEARS_SCENARIO, page_path)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        reasoner = NamingReasoner()
        page = await _compile_on_page(browser, path, reasoner)
        values = await _values(page)
        await page.context.close()
        await browser.close()

    assert reasoner.calls == 2  # bez ani jednej powtórki
    assert reasoner.feedback == []

    _, od, do = _targets(path)
    assert (od.role, od.name, od.nth) == ("textbox", "", 0)
    assert (do.role, do.name, do.nth) == ("textbox", "", 1)
    # Regresja na najbardziej wymowny przypadek ze zgłoszenia: dwa różne kroki
    # nie mogą dostać jednego namiaru.
    assert od != do

    assert values["rok-od"] == "2022"
    assert values["rok-do"] == "2024"
    assert values["tytul-wartosc"] == ""

    # Zamrożona tożsamość niesie ścieżkę DOM — bez niej wykrywanie dryfu przy
    # następnej kompilacji milczy z definicji.
    compiled = load_compiled(compiled_path(path))
    digests = [action.identity.dom_path_digest for action in compiled.actions[1:3]]
    assert all(digest is not None for digest in digests)
    assert digests[0] != digests[1]

    # Namiar pozycyjny zostaje kruchy, więc kompilacja mówi o tym głośno —
    # z liczbą trafień, żeby autor wiedział, jak bardzo.
    out = capsys.readouterr().out
    assert "namiar pozycyjny (1 z 4 pasujących, nth=0)" in out
    assert "namiar pozycyjny (2 z 4 pasujących, nth=1)" in out


# --- ścieżka produkcyjna: bramka CLI i dryf ---------------------------------


async def test_gate_stays_true_when_no_positional_index_was_frozen(tmp_path: Path) -> None:
    """Kontrola dla testu niżej: bramka nie jest wyłączona na ślepo.

    Bez tego „``compile_up_to_date`` jest ``False``" nie znaczyłoby nic — mogłoby
    być ``False`` zawsze. Scenariusz celujący w jednoznaczny przycisk nadal
    oszczędza uruchomienie przeglądarki.
    """

    page_path = _install_page(tmp_path)
    path = _write_scenario(tmp_path, "szukaj.scenario.yaml", UNIQUE_SCENARIO, page_path)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await _compile_on_page(browser, path, NamingReasoner())
        await page.context.close()
        await browser.close()

    assert _targets(path)[1].nth is None
    assert compile_up_to_date(path) is True


async def test_drift_reresolves_through_the_gate_the_cli_uses(tmp_path: Path) -> None:
    """Dryf zamrożonego indeksu, przejściem przez tę samą bramkę, co CLI.

    Test celowo naśladuje `guidebot compile`: najpierw pyta
    :func:`compile_up_to_date` (CLI kończy tu pracę, gdy odpowiedź brzmi „tak",
    i **nie otwiera przeglądarki**), a dopiero potem woła :func:`run_compile`.
    Gdyby szedł przez ``run_compile_in_browser``, ominąłby dokładnie ten bloker,
    który naprawiamy: odcisk kroku nie zmienia się od przebudowy strony, więc
    bez wyjątku dla ``nth`` wykrywanie dryfu byłoby martwe na jedynej ścieżce,
    którą używa człowiek.

    Zmiana strony między kompilacjami to **nienazwane pole doklejone do wiersza
    „Szukana fraza"** — czyli do wiersza *innego* niż cel. To nie jest kaprys:
    ``dom_path_digest`` jest ścieżką pozycyjną i absolutną, więc dołożenie
    strukturalnie IDENTYCZNEGO wiersza przed „Zakresem lat" nie dałoby żadnego
    sygnału — nowy wiersz zająłby tę samą pozycję strukturalną, jaką miał
    zamrożony cel, a więc miałby identyczną ścieżkę i identyczny skrót (spec:
    „Ograniczenie: co ten sygnał łapie, a czego nie"). Wstrzyknięcie do wiersza,
    który już ma jedno pole, przesuwa listę trafień o jeden, ale nowy okupant
    indeksu 0 leży pod inną ścieżką — i to jest wariant, który sygnał faktycznie
    łapie.
    """

    page_path = _install_page(tmp_path)
    path = _write_scenario(tmp_path, "lata.scenario.yaml", YEARS_SCENARIO, page_path)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        first = NamingReasoner()
        page = await _compile_on_page(browser, path, first)
        await page.context.close()
        assert first.calls == 2

        # Bramka, na której CLI kończy pracę bez przeglądarki. Zamrożony `nth`
        # musi ją otworzyć — inaczej dalszy ciąg tego testu nigdy by się nie
        # wykonał na prawdziwej ścieżce.
        assert compile_up_to_date(path) is False

        # Strona się przebudowała. Odcisk kroku (wersja kompilatora, rodzaj
        # komendy, treść, hash configu, stan) jest identyczny, więc `_can_reuse`
        # nadal mówi „reuse".
        _install_page(tmp_path, extra_field=True)

        context = await browser.new_context(viewport={"width": 800, "height": 600})
        probe = await context.new_page()
        await probe.goto(page_path.resolve().as_uri())

        cached = load_compiled(compiled_path(path)).actions[1]
        # Kontrola tożsamości sama by tego nie wyłapała: nowe pole ma ten sam
        # `tag` i ten sam `ancestry_digest`, bo wszystkie wiersze są zbudowane
        # tak samo. Dokładnie ta ślepota jest treścią zgłoszenia — i dokładnie
        # dlatego dryf musi być osobnym sygnałem.
        assert await reuse_is_valid(probe, cached) is True
        assert await pinned_drifted(probe, cached) is True
        await context.close()

        second = NamingReasoner()
        page = await _compile_on_page(browser, path, second)
        values = await _values(page)
        await page.context.close()
        await browser.close()

    # Oba kroki przeszły ponowną rezolucję, bo oba zamrożone indeksy dryfnęły.
    assert second.calls == 2
    _, od, do = _targets(path)
    assert od.nth == 1
    assert do.nth == 2
    # ...i wartości znowu są tam, gdzie mówi scenariusz — mimo przesuniętych
    # indeksów. Dodatkowe pole zostało puste.
    assert values["fraza-dodatkowa"] == ""
    assert values["rok-od"] == "2022"
    assert values["rok-do"] == "2024"


# --- pełny cykl compile → render --------------------------------------------


class DriveSpy:
    """Zapisz, w który element trafił każdy krok renderu.

    Kontekst renderu jest zamykany, zanim ``run_render`` wróci, więc po fakcie
    nie da się już nic odczytać ze strony. Opakowanie dwóch wejść
    :class:`Recorder` trzyma obserwację wewnątrz kroku, do którego należy.
    """

    def __init__(self) -> None:
        self.driven: list[tuple[str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self
        original_enter_text = Recorder.enter_text
        original_click = Recorder.click

        # Krzyżyk nie ma `id` — oba wyglądają identycznie — więc dla niego
        # rozróżnikiem jest etykieta wiersza, w którym siedzi.
        _WHICH = (
            "el => el.id || (el.closest('.row')?.querySelector('span, label')"
            "?.textContent || '').trim()"
        )

        async def record(recorder: Recorder, kind: str, target) -> None:
            locator = await build_locator(recorder.frame, target)
            spy.driven.append((kind, await locator.evaluate(_WHICH)))

        async def enter_text(self, target, text):
            await record(self, "type", target)
            await original_enter_text(self, target, text)

        async def click(self, target, *, before_click=None):
            await record(self, "click", target)
            await original_click(self, target, before_click=before_click)

        monkeypatch.setattr(Recorder, "enter_text", enter_text)
        monkeypatch.setattr(Recorder, "click", click)


@pytest.mark.ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe niedostępne",
)
async def test_pinned_targets_drive_the_right_elements_through_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zamrożone namiary napędzają w renderze te elementy, które opisał autor.

    Dopiero to zamyka pętlę: `compile` zmierzył indeksy na swojej sesji, a
    `render` odtwarza je na nowej — bez pytania reasonera o cokolwiek. Krzyżyk
    jest tu nie dla ozdoby: to druga niejednoznaczna kontrolka ze zgłoszenia
    (nazwa dostępna pusta, bo ikona jest ``aria-hidden``), a jej efekt uboczny —
    usunięcie wiersza — jest jedynym sposobem, żeby po akcji stwierdzić, *który*
    z dwóch identycznych przycisków dostał kliknięcie.
    """

    page_path = _install_page(tmp_path)
    path = _write_scenario(tmp_path, "kryteria.scenario.yaml", FULL_CYCLE_SCENARIO, page_path)
    spy = DriveSpy()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        reasoner = NamingReasoner()
        page = await _compile_on_page(browser, path, reasoner)
        after_compile = await _rows(page)
        await page.context.close()
        assert reasoner.calls == 3

        # Kompilacja kliknęła krzyżyk w ostatnim wierszu kryterium — i zniknął
        # właśnie ten wiersz, nie „Tytuł zawiera".
        assert after_compile == ["Szukana fraza", "Zakres lat", "Tytuł zawiera"]

        spy.install(monkeypatch)
        out = tmp_path / "kryteria.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    # Render nie pytał reasonera (atrapa nie została podana do `run_render`
    # w ogóle) i trafił w te same trzy elementy, co kompilacja.
    assert spy.driven == [
        ("type", "rok-od"),
        ("type", "rok-do"),
        ("click", "Autor zawiera"),
    ]

    assert out.exists()
    assert probe_duration(out) > 0
