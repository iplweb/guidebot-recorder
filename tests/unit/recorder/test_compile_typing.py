"""`compile` typing/`enterText` behaviour and secret redaction.

Covers reprompting for missing/invented input text, refusing to type into a
password field or with a sensitive literal, redacting `${ENV}` values out of
runtime error messages and verbose logs, and the absent-step warning banner.
"""

import textwrap

import pytest

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.target import LabelTarget, RoleTarget
from guidebot_recorder.recorder._debug import scenario_sensitive_values
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.resolution import MAX_REPROMPT
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references

from ._compile_helpers import MockReasoner, make_page

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


@pytest.fixture
async def page():
    async for pg in make_page():
        yield pg


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
