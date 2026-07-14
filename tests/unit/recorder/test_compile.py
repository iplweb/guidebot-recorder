import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.compile import _short, run_compile
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled

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

    compiled = load_compiled(compiled_path(path))
    ca = compiled.actions[1]
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


async def test_compile_sets_viewport_from_config(tmp_path, page):
    # config.viewport = 800x600; compile MUSI go ustawić (spójność z render)
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    await run_compile(path, page, MockReasoner())

    assert page.viewport_size == {"width": 800, "height": 600}


async def test_compile_force_reresolves(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    first = MockReasoner()
    await run_compile(path, page, first)
    assert first.calls == 1

    forced = MockReasoner()
    await run_compile(path, page, forced, force=True)
    assert forced.calls == 1  # --force ignoruje cache i woła reasonera ponownie


async def test_compile_navigates_with_object_form_and_ignores_render_type_flag(tmp_path, page):
    path = tmp_path / "object-navigate.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Object navigate
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate:
                  url: "data:text/html,<h1>Object navigation</h1>"
                  type: true
            """
        ),
        encoding="utf-8",
    )
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner)

    assert await page.get_by_role("heading", name="Object navigation").count() == 1
    assert reasoner.calls == 0


def test_compile_short_description_uses_object_navigate_url():
    step = Step.model_validate(
        {"navigate": {"url": "https://example.com/login", "type": True}}
    )

    assert _short(step) == "https://example.com/login"
