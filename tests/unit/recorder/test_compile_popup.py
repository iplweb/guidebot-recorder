"""`compile` popup adoption: when a click may open exactly one extra window.

Covers popups that appear too early (during reasoning or click preparation) or
too late (after the discovery deadline), the raw `_wait_for_new_pages`
quiescence rule, and the "at most one popup per session / exactly one per click"
guards.
"""

import asyncio
import textwrap

import pytest

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import _wait_for_new_pages, run_compile
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import ReasonerResult

from ._compile_helpers import MockReasoner, make_page


@pytest.fixture
async def page():
    async for pg in make_page():
        yield pg


async def test_popup_opened_during_reasoning_is_unexpected_and_click_is_not_run(tmp_path, page):
    path = tmp_path / "unexpected-popup.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Popup
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<button onclick='this.dataset.clicked=1'>Cel</button>"
              - teach: "kliknij Cel"
            """
        ),
        encoding="utf-8",
    )

    class PopupDuringReasoning:
        async def resolve(self, instruction, candidates):
            await page.evaluate("() => { window.open('about:blank'); }")
            return ReasonerResult("click", RoleTarget(role="button", name="Cel", exact=True))

    with pytest.raises(RuntimeError, match="podczas rozwiązywania.*przed akcją click"):
        await run_compile(path, page, PopupDuringReasoning(), selects=None)

    assert await page.get_by_role("button", name="Cel").get_attribute("data-clicked") is None


async def test_popup_opened_during_click_preparation_is_not_attributed(tmp_path, page, monkeypatch):
    path = tmp_path / "pre-click-popup.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Popup
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<button onclick='this.dataset.clicked=1'>Cel</button>"
              - teach: "kliknij Cel"
            """
        ),
        encoding="utf-8",
    )
    original_prepare = Recorder._point_and_prepare

    async def prepare_and_open_popup(self, target, *, click_sound=False):
        locator = await original_prepare(self, target, click_sound=click_sound)
        await self.page.evaluate("() => window.open('about:blank')")
        await self.page.wait_for_timeout(50)
        return locator

    monkeypatch.setattr(Recorder, "_point_and_prepare", prepare_and_open_popup)

    class TargetReasoner:
        async def resolve(self, instruction, candidates):
            return ReasonerResult("click", RoleTarget(role="button", name="Cel", exact=True))

    with pytest.raises(RuntimeError, match="przed akcją click"):
        await run_compile(path, page, TargetReasoner(), selects=None)

    assert await page.get_by_role("button", name="Cel").get_attribute("data-clicked") is None


async def test_popup_after_click_discovery_deadline_is_unexpected(tmp_path, page, monkeypatch):
    main = tmp_path / "late-popup.html"
    main.write_text(
        "<button onclick=\"setTimeout(() => window.open('about:blank'), 1100)\">Zaloguj</button>",
        encoding="utf-8",
    )
    path = tmp_path / "late-popup.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: Late popup
              viewport: {{width: 800, height: 600}}
              tts: {{provider: edge, voice: v, lang: pl-PL}}
            steps:
              - navigate: "{main.resolve().as_uri()}"
              - teach: "kliknij Zaloguj"
            """
        ),
        encoding="utf-8",
    )
    original_readiness = Recorder.apply_readiness

    async def delayed_readiness(self, expect):
        if expect == "none":
            await asyncio.sleep(1.2)
            return
        await original_readiness(self, expect)

    monkeypatch.setattr(Recorder, "apply_readiness", delayed_readiness)

    with pytest.raises(RuntimeError, match="nieoczekiwany dodatkowy popup"):
        await run_compile(path, page, MockReasoner(), selects=None)


async def test_popup_quiescence_never_extends_hard_discovery_deadline():
    main = object()
    inside = object()
    late = object()

    class Context:
        pages = [main]

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    found = await _wait_for_new_pages(
        Context(),
        (main,),
        [main, inside, late],
        1,
        {
            main: started_at,
            inside: started_at + 0.04,
            late: started_at + 0.06,
        },
        started_at=started_at,
        timeout=0.05,
    )

    assert found == [inside]


async def test_compile_rejects_second_popup_in_session(tmp_path, page):
    second = tmp_path / "second.html"
    second.write_text("<h1>Drugi popup</h1>", encoding="utf-8")
    first = tmp_path / "first.html"
    first.write_text(
        "<button onclick=\"window.open('second.html')\">Otwórz drugi</button>",
        encoding="utf-8",
    )
    main = tmp_path / "main.html"
    main.write_text(
        "<button onclick=\"window.open('first.html')\">Otwórz pierwszy</button>",
        encoding="utf-8",
    )
    scenario = textwrap.dedent(
        f"""\
        config:
          title: t
          viewport: {{width: 800, height: 600}}
          tts: {{provider: edge, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{main.resolve().as_uri()}"
          - teach: "otwórz pierwszy popup"
          - teach: "otwórz drugi popup"
        """
    )
    path = tmp_path / "two-popups.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")

    class TwoPopupReasoner:
        async def resolve(self, instruction, candidates):
            name = "Otwórz pierwszy" if "pierwszy" in instruction else "Otwórz drugi"
            return ReasonerResult("click", RoleTarget(role="button", name=name, exact=True))

    with pytest.raises(RuntimeError, match="co najwyżej jeden popup"):
        await run_compile(path, page, TwoPopupReasoner(), selects=None)


async def test_compile_rejects_second_sequential_popup_after_first_closes(tmp_path, page):
    first = tmp_path / "first.html"
    first.write_text(
        '<button onclick="window.close()">Zamknij pierwszy</button>',
        encoding="utf-8",
    )
    second = tmp_path / "second.html"
    second.write_text("<h1>Drugi popup</h1>", encoding="utf-8")
    main = tmp_path / "main.html"
    main.write_text(
        "<button onclick=\"window.open('first.html')\">Otwórz pierwszy</button>"
        "<button onclick=\"window.open('second.html')\">Otwórz drugi</button>",
        encoding="utf-8",
    )
    path = tmp_path / "sequential-popups.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: t
              viewport: {{width: 800, height: 600}}
              tts: {{provider: edge, voice: v, lang: pl-PL}}
            steps:
              - navigate: "{main.resolve().as_uri()}"
              - teach: "otwórz pierwszy popup"
              - click: "Zamknij pierwszy popup"
              - teach: "otwórz drugi popup"
            """
        ),
        encoding="utf-8",
    )

    class SequentialPopupReasoner:
        async def resolve(self, instruction, candidates):
            if "Zamknij" in instruction:
                name = "Zamknij pierwszy"
            elif "pierwszy" in instruction:
                name = "Otwórz pierwszy"
            else:
                name = "Otwórz drugi"
            return ReasonerResult("click", RoleTarget(role="button", name=name, exact=True))

    with pytest.raises(RuntimeError, match="co najwyżej jeden popup"):
        await run_compile(path, page, SequentialPopupReasoner(), selects=None)


async def test_compile_rejects_two_popups_opened_by_one_click(tmp_path, page):
    main = tmp_path / "main.html"
    main.write_text(
        "<button onclick=\"window.open('about:blank'); window.open('about:blank')\">"
        "Otwórz dwa</button>",
        encoding="utf-8",
    )
    path = tmp_path / "simultaneous-popups.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: t
              viewport: {{width: 800, height: 600}}
              tts: {{provider: edge, voice: v, lang: pl-PL}}
            steps:
              - navigate: "{main.resolve().as_uri()}"
              - teach: "otwórz dwa popupy"
            """
        ),
        encoding="utf-8",
    )

    class TwoAtOnceReasoner:
        async def resolve(self, instruction, candidates):
            return ReasonerResult("click", RoleTarget(role="button", name="Otwórz dwa", exact=True))

    with pytest.raises(RuntimeError, match="dokładnie jeden popup"):
        await run_compile(path, page, TwoAtOnceReasoner(), selects=None)
