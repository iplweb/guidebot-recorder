import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.loader import load_scenario

SCENARIO = textwrap.dedent(
    """\
    config:
      title: Logowanie
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<button>Zaloguj</button>"
      - teach: "kliknij Zaloguj"
    """
)


class MockReasoner:
    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Zaloguj", exact=True),
        )


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_compile_fills_cached_action(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner)

    loaded = load_scenario(path)
    ca = loaded.scenario.steps[1].cached_action
    assert ca is not None
    assert ca.action == "click"
    assert isinstance(ca.target, RoleTarget)
    assert ca.target.name == "Zaloguj"
    assert ca.identity.tag == "button"
    assert ca.fingerprint.config_hash  # niepusty
    assert reasoner.calls == 1


async def test_recompile_reuses_cache_without_reasoner(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    first = MockReasoner()
    await run_compile(path, page, first)
    assert first.calls == 1

    second = MockReasoner()
    await run_compile(path, page, second)
    assert second.calls == 0  # reuse — LLM nie wołany
