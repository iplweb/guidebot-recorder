import asyncio
import textwrap

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import LabelTarget, RoleTarget
from guidebot_recorder.recorder.compile import (
    _short,
    _wait_for_new_pages,
    compile_up_to_date,
    run_compile,
)
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled

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

TEACH_TYPE_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Wpisywanie
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<label for=email>E-mail</label><input id=email>"
      - teach: "wpisz demo@example.com w pole E-mail"
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


async def test_editing_translation_does_not_invalidate_canonical_teach(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Logowanie
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: pl, lang: pl-PL, trackLanguage: pol}
          audioTracks:
            - {provider: edge, voice: en, lang: en-US, trackLanguage: eng}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - teach: "kliknij Zaloguj"
            translations: {en-US: "Click Log in"}
        """
    )
    path = tmp_path / "multilingual.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    await run_compile(path, page, MockReasoner())

    path.write_text(scenario.replace("Click Log in", "Choose Log in"), encoding="utf-8")

    assert compile_up_to_date(path) is True
    reasoner = MockReasoner()
    await run_compile(path, page, reasoner)
    assert reasoner.calls == 0
    action = load_compiled(compiled_path(path)).actions[1]
    assert action is not None
    assert action.fingerprint.compiled_from == "kliknij Zaloguj"


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
    step = Step.model_validate({"navigate": {"url": "https://example.com/login", "type": True}})

    assert _short(step) == "https://example.com/login"


async def test_teach_type_reprompts_missing_input_text_before_filling(tmp_path, page):
    path = tmp_path / "type.scenario.yaml"
    path.write_text(TEACH_TYPE_SCENARIO, encoding="utf-8")
    target = RoleTarget(role="textbox", name="E-mail", exact=True)

    class RetryingReasoner:
        def __init__(self):
            self.calls = 0

        async def resolve(self, instruction, candidates):
            self.calls += 1
            if self.calls == 1:
                return ReasonerResult("type", target)
            return ReasonerResult("type", target, input_text="demo@example.com")

    reasoner = RetryingReasoner()
    await run_compile(path, page, reasoner)

    compiled = load_compiled(compiled_path(path))
    action = compiled.actions[1]
    assert reasoner.calls == 2
    assert action is not None and action.input_text == "demo@example.com"
    assert await page.locator("#email").input_value() == "demo@example.com"


@pytest.mark.parametrize(
    ("input_text", "message"),
    [
        (None, "nie zwrócił niepustego inputText"),
        ("fabricated@example.com", "nie jest literalnym fragmentem"),
    ],
)
async def test_teach_type_rejects_missing_or_invented_text_after_reprompts(
    tmp_path,
    page,
    input_text,
    message,
):
    path = tmp_path / "invalid-type.scenario.yaml"
    path.write_text(TEACH_TYPE_SCENARIO, encoding="utf-8")
    target = RoleTarget(role="textbox", name="E-mail", exact=True)

    class InvalidReasoner:
        def __init__(self):
            self.calls = 0

        async def resolve(self, instruction, candidates):
            self.calls += 1
            return ReasonerResult("type", target, input_text=input_text)

    reasoner = InvalidReasoner()
    with pytest.raises(RuntimeError, match=message):
        await run_compile(path, page, reasoner)

    assert reasoner.calls == 2
    assert await page.locator("#email").input_value() == ""


@pytest.mark.parametrize(
    "instruction",
    ["wpisz hasło hunter2 w pole E-mail", "Wklej hasło hunter2 do pola E-mail"],
)
async def test_teach_type_rejects_sensitive_literal_before_reasoner_or_log(
    tmp_path, page, capsys, instruction
):
    path = tmp_path / "sensitive-type.scenario.yaml"
    path.write_text(
        TEACH_TYPE_SCENARIO.replace(
            "wpisz demo@example.com w pole E-mail",
            instruction,
        ),
        encoding="utf-8",
    )
    target = RoleTarget(role="textbox", name="E-mail", exact=True)

    class SensitiveReasoner:
        def __init__(self):
            self.calls = 0

        async def resolve(self, instruction, candidates):
            self.calls += 1
            return ReasonerResult("type", target, input_text="hunter2")

    reasoner = SensitiveReasoner()
    with pytest.raises(RuntimeError, match="wartości wrażliwe"):
        await run_compile(path, page, reasoner, verbose=True)

    assert reasoner.calls == 0
    assert await page.locator("#email").input_value() == ""
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err


async def test_teach_type_rejects_password_dom_target_before_typing(tmp_path, page):
    path = tmp_path / "password-target.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Wpisywanie
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<label for=value>Access</label><input id=value type=password>"
              - teach: "wpisz hunter2 w pole Access"
            """
        ),
        encoding="utf-8",
    )

    class PasswordReasoner:
        def __init__(self):
            self.calls = 0

        async def resolve(self, instruction, candidates):
            self.calls += 1
            return ReasonerResult(
                "type",
                LabelTarget(label="Access", exact=True),
                input_text="hunter2",
            )

    reasoner = PasswordReasoner()
    with pytest.raises(RuntimeError, match="pole wygląda na przeznaczone"):
        await run_compile(path, page, reasoner)

    assert reasoner.calls == 2
    assert await page.locator("#value").input_value() == ""


async def test_enter_text_runtime_error_redacts_value_from_exception_and_verbose_log(
    tmp_path,
    page,
    monkeypatch,
    capsys,
):
    secret = "sentinel-fill-secret"
    path = tmp_path / "explicit-type.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Wpisywanie
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<label for=email>E-mail</label><input id=email>"
              - enterText: {into: "pole E-mail", text: "${PASSWORD}"}
            """
        ),
        encoding="utf-8",
    )

    class TypeReasoner:
        async def resolve(self, instruction, candidates):
            return ReasonerResult(
                "type",
                RoleTarget(role="textbox", name="E-mail", exact=True),
            )

    async def fail_with_playwright_style_message(self, target, text):
        assert text == secret
        raise RuntimeError(f'locator.fill("{text}") timed out')

    monkeypatch.setattr(Recorder, "enter_text", fail_with_playwright_style_message)

    with pytest.raises(RuntimeError) as captured:
        await run_compile(
            path,
            page,
            TypeReasoner(),
            {"PASSWORD": secret},
            verbose=True,
        )

    diagnostics = str(captured.value) + capsys.readouterr().out
    assert secret not in diagnostics
    assert "<redacted>" in diagnostics


async def test_navigate_runtime_error_redacts_expanded_env_value(
    tmp_path,
    page,
    monkeypatch,
    capsys,
):
    secret = "sentinel-navigation-token"
    path = tmp_path / "secret-navigation.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Nawigacja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "https://example.test/login?token=${TOKEN}"
            """
        ),
        encoding="utf-8",
    )

    async def fail_with_effective_url(self, url):
        assert secret in url
        raise RuntimeError(f'page.goto("{url}") timed out')

    monkeypatch.setattr(Recorder, "navigate", fail_with_effective_url)

    with pytest.raises(RuntimeError) as captured:
        await run_compile(
            path,
            page,
            MockReasoner(),
            {"TOKEN": secret},
            verbose=True,
        )

    diagnostics = str(captured.value) + capsys.readouterr().out
    assert secret not in diagnostics
    assert "<redacted>" in diagnostics


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
        await run_compile(path, page, PopupDuringReasoning())

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

    async def prepare_and_open_popup(self, target):
        locator = await original_prepare(self, target)
        await self.page.evaluate("() => window.open('about:blank')")
        await self.page.wait_for_timeout(50)
        return locator

    monkeypatch.setattr(Recorder, "_point_and_prepare", prepare_and_open_popup)

    class TargetReasoner:
        async def resolve(self, instruction, candidates):
            return ReasonerResult("click", RoleTarget(role="button", name="Cel", exact=True))

    with pytest.raises(RuntimeError, match="przed akcją click"):
        await run_compile(path, page, TargetReasoner())

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
        await run_compile(path, page, MockReasoner())


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


async def test_old_compiler_version_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner())

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    stale = compiled.model_copy(update={"compiler_version": COMPILER_VERSION - 1})
    write_compiled(cpath, stale)

    assert compile_up_to_date(path) is False


async def test_old_action_fingerprint_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner())

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    stale_fingerprint = action.fingerprint.model_copy(
        update={"compiler_version": COMPILER_VERSION - 1}
    )
    stale_action = action.model_copy(update={"fingerprint": stale_fingerprint})
    stale = compiled.model_copy(update={"actions": [None, stale_action]})
    write_compiled(cpath, stale)

    assert compile_up_to_date(path) is False


def test_targetless_scenario_requires_current_aligned_sidecar(tmp_path):
    path = tmp_path / "narration.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Narracja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - say: "Cześć"
            """
        ),
        encoding="utf-8",
    )
    cpath = compiled_path(path)

    assert compile_up_to_date(path) is False

    current = CompiledScenario(source=path.name, actions=[None])
    write_compiled(cpath, current)
    assert compile_up_to_date(path) is True

    write_compiled(cpath, current.model_copy(update={"source": "other.scenario.yaml"}))
    assert compile_up_to_date(path) is False

    write_compiled(cpath, current)

    write_compiled(
        cpath,
        current.model_copy(update={"compiler_version": COMPILER_VERSION - 1}),
    )
    assert compile_up_to_date(path) is False

    write_compiled(cpath, current.model_copy(update={"actions": []}))
    assert compile_up_to_date(path) is False


async def test_target_step_changed_to_say_requires_compile(tmp_path, page):
    path = tmp_path / "changed-kind.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner())

    path.write_text(
        SCENARIO.replace('- teach: "kliknij Zaloguj"', '- say: "To już narracja"'),
        encoding="utf-8",
    )

    assert compile_up_to_date(path) is False


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
        await run_compile(path, page, TwoPopupReasoner())


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
        await run_compile(path, page, SequentialPopupReasoner())


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
        await run_compile(path, page, TwoAtOnceReasoner())
