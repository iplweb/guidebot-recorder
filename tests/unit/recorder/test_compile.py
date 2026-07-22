import asyncio
import textwrap

import pytest
from playwright.async_api import async_playwright

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.action import COMPILER_VERSION
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import LabelTarget, RoleTarget, TextTarget
from guidebot_recorder.recorder._debug import scenario_sensitive_values
from guidebot_recorder.recorder.compile import (
    _short,
    _wait_for_new_pages,
    compile_up_to_date,
    needs_positional_recheck,
    run_compile,
)
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.resolution import MAX_REPROMPT
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references

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

    await run_compile(path, page, reasoner, selects=None)

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
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    second = MockReasoner()
    await run_compile(path, page, second, selects=None)
    assert second.calls == 0  # reuse — LLM nie wołany


async def test_recompile_reuses_cache_without_rewriting_unchanged_sidecar(
    tmp_path, page, monkeypatch
):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    writes = 0
    original_write = compile_module.run.write_compiled

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(compile_module.run, "write_compiled", count_write)

    await run_compile(path, page, MockReasoner(), selects=None)
    assert writes == 1  # fresh resolve checkpoint; navigate does not rewrite the sidecar

    writes = 0
    reasoner = MockReasoner()
    await run_compile(path, page, reasoner, selects=None)

    assert reasoner.calls == 0
    assert writes == 0


async def test_fresh_resolution_is_checkpointed_before_a_later_failure(tmp_path, page):
    path = tmp_path / "partial.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Częściowa kompilacja
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - navigate: "data:text/html,<button>Pierwszy</button><button>Drugi</button>"
              - teach: "kliknij Pierwszy"
              - teach: "kliknij Drugi"
            """
        ),
        encoding="utf-8",
    )

    class FailsSecondResolution:
        async def resolve(self, instruction, candidates):
            if "Drugi" in instruction:
                raise RuntimeError("synthetic second-step failure")
            return ReasonerResult(
                action="click",
                target=RoleTarget(role="button", name="Pierwszy", exact=True),
            )

    with pytest.raises(RuntimeError, match="synthetic second-step failure"):
        await run_compile(path, page, FailsSecondResolution(), selects=None)

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions[1] is not None
    assert compiled.actions[2] is None


async def test_targetless_compile_still_writes_final_aligned_sidecar(tmp_path, page, monkeypatch):
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
    writes = 0
    original_write = compile_module.run.write_compiled

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(compile_module.run, "write_compiled", count_write)

    await run_compile(path, page, MockReasoner(), selects=None)

    compiled = load_compiled(compiled_path(path))
    assert compiled.actions == [None]
    assert writes == 1


async def test_empty_scenario_still_does_not_create_sidecar(tmp_path, page):
    path = tmp_path / "empty.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Pusty scenariusz
              viewport: {width: 800, height: 600}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps: []
            """
        ),
        encoding="utf-8",
    )

    await run_compile(path, page, MockReasoner(), force=True, selects=None)

    assert not compiled_path(path).exists()


async def test_slide_compiles_to_null_without_reasoner(tmp_path, page):
    scenario = textwrap.dedent(
        """\
        config:
          title: Slajd
          viewport: {width: 800, height: 600}
          tts: {provider: edge, voice: v, lang: pl-PL}
        steps:
          - navigate: "data:text/html,<button>Zaloguj</button>"
          - slide:
              title: "Krok 1"
              subtitle: "Kliknij przycisk"
          - teach: "kliknij Zaloguj"
        """
    )
    path = tmp_path / "slide.scenario.yaml"
    path.write_text(scenario, encoding="utf-8")
    reasoner = MockReasoner()

    await run_compile(path, page, reasoner, selects=None)

    compiled = load_compiled(compiled_path(path))
    assert len(compiled.actions) == 3  # jeden slot na krok — również dla slide
    assert compiled.actions[1] is None  # slide → null cached action, bez Reasonera
    assert compiled.actions[2] is not None  # kolejny krok (teach) rozwiązany normalnie
    assert reasoner.calls == 1  # Reasoner wołany tylko dla kroku teach, nie dla slide


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
    await run_compile(path, page, MockReasoner(), selects=None)

    path.write_text(scenario.replace("Click Log in", "Choose Log in"), encoding="utf-8")

    assert compile_up_to_date(path) is True
    reasoner = MockReasoner()
    await run_compile(path, page, reasoner, selects=None)
    assert reasoner.calls == 0
    action = load_compiled(compiled_path(path)).actions[1]
    assert action is not None
    assert action.fingerprint.compiled_from == "kliknij Zaloguj"


async def test_compile_sets_viewport_from_config(tmp_path, page):
    # config.viewport = 800x600; compile MUSI go ustawić (spójność z render)
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    await run_compile(path, page, MockReasoner(), selects=None)

    assert page.viewport_size == {"width": 800, "height": 600}


async def test_compile_force_reresolves(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    first = MockReasoner()
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    forced = MockReasoner()
    await run_compile(path, page, forced, force=True, selects=None)
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

    await run_compile(path, page, reasoner, selects=None)

    assert await page.get_by_role("heading", name="Object navigation").count() == 1
    assert reasoner.calls == 0


def test_compile_short_description_uses_object_navigate_url():
    step = Step.model_validate({"navigate": {"url": "https://example.com/login", "type": True}})

    assert _short(step) == "https://example.com/login"


def test_compile_short_description_uses_slide_title():
    step = Step.model_validate({"slide": {"title": "Krok 1", "subtitle": "Kliknij przycisk"}})

    assert _short(step) == "Krok 1"


def test_compile_short_description_falls_back_to_slide_subtitle():
    step = Step.model_validate({"slide": {"subtitle": "Kliknij przycisk"}})

    assert _short(step) == "Kliknij przycisk"


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
    await run_compile(path, page, reasoner, selects=None)

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
        await run_compile(path, page, reasoner, selects=None)

    assert reasoner.calls == MAX_REPROMPT
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
        await run_compile(path, page, reasoner, verbose=True, selects=None)

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
        await run_compile(path, page, reasoner, selects=None)

    assert reasoner.calls == MAX_REPROMPT
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
            selects=None,
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
            selects=None,
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


async def test_old_compiler_version_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    stale = compiled.model_copy(update={"compiler_version": COMPILER_VERSION - 1})
    write_compiled(cpath, stale)

    assert compile_up_to_date(path) is False


async def test_old_action_fingerprint_is_not_up_to_date(tmp_path, page):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

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
    await run_compile(path, page, MockReasoner(), selects=None)

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


async def test_close_window_compiles_to_null_and_returns_to_main(tmp_path, page):
    # A data: page cannot open a new window onto another data: URL (Chromium
    # blocks it outright, for either an <a target=_blank> or window.open), so
    # the popup destination needs a real file:// URL, matching the convention
    # used by the other popup tests in this file.
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


SECRET_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Sekret
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<p>pusto</p>"
      - enterText:
          into: "pole logowania"
          text: "${SECRET}"
      - teach: "kliknij menu uzytkownika hunter2"
        optional: true
    """
)


def test_warn_absent_redacts_env_secrets(tmp_path, capsys):
    """Ostrzeżenie runtime nie może wypisać wartości wstrzykniętej przez `${ENV}`.

    Snippet YAML jest bezpieczny z definicji (pochodzi sprzed podstawienia), ale
    treść komunikatu — `_instruction(step)` — powtarza tu tę samą wartość, którą
    scenariusz wpisuje z `${SECRET}`. Bez redakcji całego bannera sekret
    wyciekłby wierszem pod fragmentem pokazującym niewinne `${SECRET}`.
    """

    path = tmp_path / "sekret.scenario.yaml"
    path.write_text(SECRET_SCENARIO, encoding="utf-8")
    env = {"SECRET": "hunter2"}
    scenario = load_scenario(path, env)
    sensitive = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    assert "hunter2" in sensitive
    flat = scenario.flat_steps()

    compile_module._warn_absent(
        2,
        flat[2].step,
        gate=False,
        total=len(flat),
        location=flat[2].location,
        source=scenario.source,
        sensitive=sensitive,
    )

    captured = capsys.readouterr()
    assert "hunter2" not in captured.out
    assert "<redacted>" in captured.out
    assert "⚠ krok 3/3 — " in captured.out
    assert "sekret.scenario.yaml:10" in captured.out

    # Ten sam banner na kroku wpisującym sekret: fragment YAML-a pokazuje surowe
    # `${SECRET}`, bo pochodzi sprzed podstawienia.
    compile_module._warn_absent(
        1,
        flat[1].step,
        gate=False,
        total=len(flat),
        location=flat[1].location,
        source=scenario.source,
        sensitive=sensitive,
    )

    typed = capsys.readouterr().out
    assert 'text: "${SECRET}"' in typed
    assert "hunter2" not in typed


def test_compile_short_description_for_highlight():
    step = Step.model_validate({"highlight": "tabela z wynikami"})

    assert _short(step) == "◯ tabela z wynikami"


AMBIGUOUS_SCENARIO = textwrap.dedent(
    """\
    config:
      title: Dwa przyciski
      viewport: {width: 800, height: 600}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - navigate: "data:text/html,<div><button>Usun</button></div><div><i>x</i></div><div><button>Usun</button></div>"
      - teach: "kliknij drugi Usun"
    """
)

#: Ta sama strona po przebudowie: drugi przycisk siedzi teraz w innym `<div>`
#: niż przy kompilacji, więc zamrożone `nth=1` trafia w element o **innej
#: pozycyjnej ścieżce DOM**. Kontrola tożsamości tego nie widzi — `tag` i
#: `ancestry_digest` (pary tag/rola, bez indeksów) są identyczne, co asertuje
#: sam test. Łańcuch przodków celowo zostaje ten sam: gdyby zmiana dokładała
#: `<section>` nad gałęzią, reuse padłby już na `ancestry_digest` i test nie
#: dowodziłby niczego o nowym sygnale.
#:
#: Jednorodne dołożenie trzeciego identycznego wiersza dryfu **nie** dałoby:
#: element trafiający w zamrożone `nth` zajmowałby tę samą pozycję strukturalną,
#: więc miałby ten sam skrót (spec, „Ograniczenie: co ten sygnał łapie, a czego
#: nie").
AMBIGUOUS_SCENARIO_MOVED = AMBIGUOUS_SCENARIO.replace(
    "<div><button>Usun</button></div><div><i>x</i></div><div><button>Usun</button></div>",
    "<div><button>Usun</button></div><div><button>Usun</button></div><div><i>x</i></div>",
)


class PickingReasoner:
    """Atrapa w nowym stylu: wskazuje kandydata, indeks liczy `compile`.

    Nie zwraca `nth` — dokładnie tak, jak po zmianie schematu widzianego przez
    model. Wybiera ostatniego kandydata o roli `button`, więc zamrożony indeks
    musi wyjść z pomiaru, a nie z arytmetyki na tablicy JSON.
    """

    def __init__(self):
        self.calls = 0

    async def resolve(self, instruction, candidates, feedback=None):
        self.calls += 1
        buttons = [candidate for candidate in candidates if candidate.role == "button"]
        return ReasonerResult(
            action="click",
            target=RoleTarget(role="button", name="Usun", exact=True),
            candidate_id=buttons[-1].id,
        )


async def test_frozen_positional_index_needs_a_recheck_but_matches_the_source(tmp_path, page):
    """Namiar pozycyjny musi otworzyć przeglądarkę — inaczej dryf jest niewykrywalny.

    Odcisk kroku (`compiler_version`, `command_kind`, `compiled_from`,
    `config_hash`, `state`) nie zmienia się od przebudowy strony, a CLI kończy
    pracę na bramce kompilacji. Pyta o to jednak osobny predykat: `nth` nie robi
    sidecara *niezgodnym ze źródłem*, a zlanie obu pytań w `compile_up_to_date`
    unieruchamiało `render-set` (świeży sidecar wiecznie „nieaktualny").
    """

    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    # regresja: cache bez `nth` nadal oszczędza uruchomienie przeglądarki
    assert compile_up_to_date(path) is True
    assert needs_positional_recheck(path) is False

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    pinned = action.model_copy(update={"target": action.target.model_copy(update={"nth": 1})})
    write_compiled(cpath, compiled.model_copy(update={"actions": [None, pinned]}))

    assert needs_positional_recheck(path) is True
    # ale sidecar nadal odpowiada źródłu — to jest pytanie preflightu renderu
    assert compile_up_to_date(path) is True


async def test_frozen_positional_index_inside_scope_needs_a_recheck(tmp_path, page):
    """`nth` bywa na targecie zagnieżdżonym w `scope` — szukamy rekurencyjnie."""

    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    await run_compile(path, page, MockReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    scoped = action.target.model_copy(
        update={"scope": RoleTarget(role="group", name="Formularz", nth=2)}
    )
    write_compiled(
        cpath,
        compiled.model_copy(
            update={"actions": [None, action.model_copy(update={"target": scoped})]}
        ),
    )

    assert needs_positional_recheck(path) is True
    assert compile_up_to_date(path) is True


async def test_positional_target_is_reused_while_the_page_holds_still(tmp_path, page):
    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")

    first = PickingReasoner()
    await run_compile(path, page, first, selects=None)
    assert first.calls == 1

    action = load_compiled(compiled_path(path)).actions[1]
    assert action.target.nth == 1  # zmierzony, nie zgadnięty
    assert action.identity.dom_path_digest is not None

    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 0  # brak dryfu → reuse jak dotąd


async def test_legacy_pinned_sidecar_is_remeasured_once_and_then_heals(tmp_path, page):
    """Stary sidecar z **zgadniętym** `nth` nie może zostać zamrożony na zawsze.

    Brak `dom_path_digest` to podpis artefaktu sprzed tej zmiany — czyli tego,
    w którym indeks pochodził z arytmetyki modelu na tablicy JSON (zgłoszenie
    #51). Bezpieczny werdykt to „zmierz jeszcze raz"; po jednym przemierzeniu
    ścieżka jest zamrożona i kolejne kompilacje porównują ją normalnie.
    """

    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")
    await run_compile(path, page, PickingReasoner(), selects=None)

    cpath = compiled_path(path)
    compiled = load_compiled(cpath)
    action = compiled.actions[1]
    legacy = action.model_copy(
        update={"identity": action.identity.model_copy(update={"dom_path_digest": None})}
    )
    write_compiled(cpath, compiled.model_copy(update={"actions": [None, legacy]}))

    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 1  # jednorazowe przemierzenie
    healed = load_compiled(cpath).actions[1]
    assert healed.identity.dom_path_digest is not None

    third = PickingReasoner()
    await run_compile(path, page, third, selects=None)

    assert third.calls == 0  # ścieżka już jest — reuse jak dla każdego innego wpisu


async def test_positional_drift_invalidates_reuse_and_reresolves(tmp_path, page):
    """Przebudowa strony nie rusza odcisku kroku, więc dryf jest jedynym sygnałem."""

    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")
    await run_compile(path, page, PickingReasoner(), selects=None)
    before = load_compiled(compiled_path(path)).actions[1]

    path.write_text(AMBIGUOUS_SCENARIO_MOVED, encoding="utf-8")
    second = PickingReasoner()
    await run_compile(path, page, second, selects=None)

    assert second.calls == 1  # dryf unieważnił wpis
    after = load_compiled(compiled_path(path)).actions[1]
    assert after.identity.dom_path_digest != before.identity.dom_path_digest
    # Sama kontrola tożsamości przepuściłaby ten wpis: `tag` i `ancestry_digest`
    # się nie zmieniły, więc `reuse_is_valid` mówiło „ważny".
    assert after.identity.ancestry_digest == before.identity.ancestry_digest


async def test_positional_target_warns_with_the_match_count(tmp_path, page, capsys):
    path = tmp_path / "ambig.scenario.yaml"
    path.write_text(AMBIGUOUS_SCENARIO, encoding="utf-8")

    await run_compile(path, page, PickingReasoner(), selects=None)

    out = capsys.readouterr().out
    # Liczebnik 1-based dla czytelnika, surowe `nth` obok — żeby autor odnalazł
    # ten sam wpis w sidecarze. Zamrożone `nth=1` to drugie z dwóch trafień.
    assert "namiar pozycyjny (2 z 2 pasujących, nth=1)" in out
    assert "rozważ doprecyzowanie opisu" in out
    assert "⚠ krok 2/2 — " in out
    assert "ambig.scenario.yaml:7" in out


async def test_unambiguous_target_does_not_warn(tmp_path, page, capsys):
    path = tmp_path / "login.scenario.yaml"
    path.write_text(SCENARIO, encoding="utf-8")

    await run_compile(path, page, MockReasoner(), selects=None)

    assert "namiar pozycyjny" not in capsys.readouterr().out


def test_target_desc_shows_nth_and_scope():
    """`--verbose` bez `nth` i `scope` ukrywałby dokładnie to, co dodajemy."""

    target = RoleTarget(
        role="button",
        name="×",
        nth=2,
        scope=TextTarget(text="Charakter formalny"),
    )

    desc = compile_module._target_desc(target)

    assert "nth=2" in desc
    assert "Charakter formalny" in desc
    assert compile_module._target_desc(RoleTarget(role="button", name="Zaloguj")) == (
        'role=button name="Zaloguj"'
    )
