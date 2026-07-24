"""`compile` handling of the `closeWindow` step."""

import textwrap

import pytest

from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import _short, run_compile
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled

from ._compile_helpers import MockReasoner, make_page


@pytest.fixture
async def page():
    async for pg in make_page():
        yield pg


async def test_close_window_compiles_to_null_and_returns_to_main(tmp_path, page):
    # A data: page cannot open a new window onto another data: URL (Chromium
    # blocks it outright, for either an <a target=_blank> or window.open), so
    # the popup destination needs a real file:// URL, matching the convention
    # used by the popup tests in test_compile_popup.py.
    second = tmp_path / "second.html"
    second.write_text("<p>druga</p>", encoding="utf-8")
    main = tmp_path / "main.html"
    main.write_text(
        f"<a href='{second.resolve().as_uri()}' target='_blank'>otworz</a>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: Karta
          viewport: {{width: 800, height: 600}}
          tts: {{provider: edge, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "kliknij otworz"
          - closeWindow: true
          - say: "Wrocilismy."
        """
    )
    path = tmp_path / "tab.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    class LinkReasoner:
        calls = 0

        async def resolve(self, instruction, candidates):
            LinkReasoner.calls += 1
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="link", name="otworz", exact=True),
            )

    await run_compile(path, page, LinkReasoner(), selects=None)

    compiled = load_compiled(compiled_path(path))
    assert len(compiled.actions) == 4  # jeden slot na krok — również dla closeWindow
    assert compiled.actions[2] is None  # closeWindow → null, bez Reasonera
    assert compiled.actions[1] is not None  # klik, który otworzył kartę
    assert compiled.actions[1].opens_popup is True


async def test_close_window_without_an_open_window_fails(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Karta
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<p>tylko glowne okno</p>"
          - closeWindow: true
        """
    )
    path = tmp_path / "bad.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    with pytest.raises(RuntimeError, match="closeWindow bez otwartego okna"):
        await run_compile(path, page, MockReasoner(), selects=None)


def test_compile_short_description_for_close_window():
    step = Step.model_validate({"closeWindow": True})

    assert _short(step) == "closeWindow"
