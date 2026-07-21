import shutil
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

import guidebot_recorder.cli as cli_module
from guidebot_recorder.cli import app
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.recorder.render_set import CompileSetResult
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled
from guidebot_recorder.scenario.loader import load_scenario

runner = CliRunner()

GOOD = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 640, height: 480}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - say: "Witaj"
    """
)

BAD_TWO_COMMANDS = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: e, voice: v, lang: pl}
    steps:
      - click: "X"
        navigate: "http://x"
    """
)


#: linia `- click: "X"` w :data:`BAD_TWO_COMMANDS`
BAD_LINE = 6


def _assert_validation_banner(result, path, *, exit_code):
    """Banner walidacji dotarł na wyjście w jednym kawałku i bez tracebacku."""

    assert result.exit_code == exit_code
    output = result.output
    assert "Traceback" not in output
    # nagłówek bannera z myślnikiem — CLI nie dokleja własnego prefiksu
    assert "BŁĄD walidacji —" in output
    assert "BŁĄD walidacji: BŁĄD" not in output
    # `plik:linia` w jednym kawałku: Rich łamał tę ścieżkę w połowie
    assert f"{path}:{BAD_LINE}" in output
    assert "^ tutaj" in output
    assert "dozwolona dokładnie jedna" in output


def _install_fake_playwright(monkeypatch):
    class FakeBrowser:
        closed = False

        async def close(self):
            self.closed = True

    browser = FakeBrowser()
    launches = []

    class FakeChromium:
        async def launch(self, *, headless):
            launches.append(headless)
            return browser

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightManager:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(cli_module, "async_playwright", FakePlaywrightManager)
    return browser, launches


def test_validate_ok(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_validate_rejects_two_commands(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code != 0


def test_compile_reports_the_banner_instead_of_a_rich_traceback(tmp_path):
    """Diagnostyka była podpięta wyłącznie do `validate`.

    `compile` — polecenie, w którym autor scenariusza ogląda swoje błędy
    najczęściej — puszczał `ScenarioValidationError` niezłapany, więc Typer
    renderował go jako panel tracebacku Richa: ścieżka pliku łamana w połowie,
    treść komunikatu przycięta do 80 kolumn.
    """

    path = tmp_path / "flow.scenario.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")

    result = runner.invoke(app, ["compile", str(path)])

    _assert_validation_banner(result, path, exit_code=2)


def test_guide_reports_the_banner_instead_of_a_rich_traceback(tmp_path, monkeypatch):
    """`guide` wczytuje scenariusz dopiero po starcie przeglądarki — banner i tak ma dojść."""

    path = tmp_path / "flow.scenario.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")
    browser, _launches = _install_fake_playwright(monkeypatch)

    result = runner.invoke(app, ["guide", str(path), "--out", str(tmp_path / "o.pdf")])

    _assert_validation_banner(result, path, exit_code=2)
    assert browser.closed is True


def test_render_reports_the_banner_instead_of_a_rich_traceback(tmp_path):
    path = tmp_path / "flow.scenario.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")

    result = runner.invoke(app, ["render", str(path), "--out", str(tmp_path / "o.mp4")])

    _assert_validation_banner(result, path, exit_code=2)


def test_setup_reports_the_banner_instead_of_a_rich_traceback(tmp_path, monkeypatch):
    """`setup` nie był na liście recenzji, ale ma dokładnie tę samą wadę."""

    path = tmp_path / "setup.scenario.yaml"
    path.write_text(BAD_TWO_COMMANDS, encoding="utf-8")
    _install_fake_playwright(monkeypatch)

    result = runner.invoke(app, ["setup", str(path)])

    _assert_validation_banner(result, path, exit_code=1)


@pytest.mark.parametrize(
    ("command", "extra"),
    [("render-set", ["--output-dir", "out"]), ("compile-set", [])],
)
def test_set_commands_pass_a_validation_banner_through_untouched(
    tmp_path, monkeypatch, command, extra
):
    """`_load_set_or_exit` nie może opakować bannera w `BŁĄD zestawu: …`.

    Banner niesie własny nagłówek i wielolinijkowy fragment YAML — prefiks
    z pierwszą linią zrósłby się w `BŁĄD zestawu: BŁĄD walidacji — …`, a reszta
    zwisałaby bez kontekstu.

    Dziś warstwa `scenario.render_set` sama zamienia każdy błąd wariantu na
    `RenderSetValidationError`, więc ta ścieżka jest asekuracyjna — testujemy ją
    wstrzykując błąd wprost, żeby obsługa w CLI nie została martwym kodem.
    """

    manifest, scenario = _write_render_set(tmp_path)
    scenario.write_text(BAD_TWO_COMMANDS, encoding="utf-8")

    def exploding_load_render_set(path):
        return load_scenario(scenario)  # rzuca ScenarioValidationError

    monkeypatch.setattr(cli_module, "load_render_set", exploding_load_render_set)

    result = runner.invoke(app, [command, str(manifest), *extra])

    _assert_validation_banner(result, scenario, exit_code=1)
    assert "BŁĄD zestawu:" not in result.output


def test_render_auto_heal_not_implemented(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")
    result = runner.invoke(
        app, ["render", str(path), "--out", str(tmp_path / "o.mp4"), "--auto-heal"]
    )
    assert result.exit_code != 0


def test_render_passes_a_reasoner_only_when_codex_is_installed(tmp_path, monkeypatch):
    """`render` stays LLM-free unless an unresolved optional branch needs healing.

    Availability is probed on the binary rather than inferred from the generic
    RuntimeError CodexReasoner raises, so a host without `codex` degrades to
    "skip the branch" instead of a failed render.
    """

    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")
    _install_fake_playwright(monkeypatch)
    seen = []

    async def fake_render(*args, **kwargs):
        seen.append(kwargs.get("reasoner"))

    monkeypatch.setattr(cli_module, "run_render", fake_render)

    monkeypatch.setattr(cli_module.shutil, "which", lambda name: None)
    runner.invoke(app, ["render", str(path), "--out", str(tmp_path / "o.mp4")])
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "/usr/local/bin/codex")
    runner.invoke(app, ["render", str(path), "--out", str(tmp_path / "o.mp4")])

    assert seen[0] is None
    assert isinstance(seen[1], cli_module.CodexReasoner)


def test_render_rejects_non_edge_provider_before_browser_launch(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(GOOD.replace("provider: edge", "provider: custom"), encoding="utf-8")

    result = runner.invoke(app, ["render", str(path), "--out", str(tmp_path / "o.mp4")])

    assert result.exit_code == 2
    assert "obsługuje provider TTS `edge`" in result.output


def test_render_rejects_bad_hold_frame_settle_before_browser_launch(tmp_path, monkeypatch):
    """`--hold-frame-settle 0` must report `BŁĄD:`/exit 2, not a raw traceback.

    It used to reach `run_render` unchecked and blow up as an unhandled
    pydantic `ValidationError` — AFTER the browser had already launched. The
    fix validates it the same way as every other `render` rejection: eagerly,
    before `async_playwright` is ever touched, so a broken `async_playwright`
    stub (nothing is patched here) never gets exercised for this case either.
    """
    path = tmp_path / "s.yaml"
    path.write_text(GOOD, encoding="utf-8")

    result = runner.invoke(
        app,
        ["render", str(path), "--out", str(tmp_path / "o.mp4"), "--hold-frame-settle", "0"],
    )

    assert result.exit_code == 2
    assert "BŁĄD: nieprawidłowa wartość --hold-frame-settle" in result.output
    assert "Traceback" not in result.output


def test_compile_success_uses_locale_aware_helper_and_closes_browser(tmp_path, monkeypatch):
    path = tmp_path / "localized.scenario.yaml"
    path.write_text(GOOD.replace("  tts:", "  locale: pl-PL\n  tts:"), encoding="utf-8")
    browser, launches = _install_fake_playwright(monkeypatch)
    reasoner = object()
    calls = []

    async def compile_in_browser(received_path, received_browser, received_reasoner, **kwargs):
        calls.append((received_path, received_browser, received_reasoner, kwargs))

    monkeypatch.setattr(cli_module, "CodexReasoner", lambda: reasoner)
    monkeypatch.setattr(cli_module, "run_compile_in_browser", compile_in_browser)

    result = runner.invoke(
        app,
        [
            "compile",
            str(path),
            "--headed",
            "--force",
            "--pause-on-error",
            "--timeout",
            "12.5",
            "--verbose",
        ],
    )

    assert result.exit_code == 0
    assert launches == [False]
    assert calls == [
        (
            path,
            browser,
            reasoner,
            {
                "timeout": 12.5,
                "force": True,
                "pause_on_error": True,
                "verbose": True,
            },
        )
    ]
    assert browser.closed is True
    assert "skompilowano" in result.output


def _write_render_set(tmp_path, *, provider="edge"):
    scenario = tmp_path / "en.scenario.yaml"
    scenario.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: English
              viewport: {{width: 640, height: 480}}
              locale: en-US
              tts:
                provider: {provider}
                voice: en
                lang: en-US
                trackLanguage: eng
            steps:
              - say: "Welcome"
            """
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "localized.render-set.yaml"
    manifest.write_text(
        textwrap.dedent(
            """\
            kind: localized-render-set
            version: 1
            variants:
              en-US:
                scenario: en.scenario.yaml
                output: en.mp4
            """
        ),
        encoding="utf-8",
    )
    return manifest, scenario


def test_render_set_requires_all_compiled_sidecars_before_browser_launch(tmp_path):
    manifest, _scenario = _write_render_set(tmp_path)

    result = runner.invoke(
        app,
        ["render-set", str(manifest), "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 2
    assert "uruchom `guidebot compile-set`" in result.output
    assert not (tmp_path / "out").exists()


def test_render_set_rejects_non_edge_provider_before_browser_launch(tmp_path):
    manifest, _scenario = _write_render_set(tmp_path, provider="custom")

    result = runner.invoke(
        app,
        ["render-set", str(manifest), "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 2
    assert "obsługuje provider TTS `edge`" in result.output


def test_compile_set_skips_browser_when_every_variant_is_current(tmp_path):
    manifest, scenario = _write_render_set(tmp_path)
    write_compiled(
        compiled_path(scenario),
        CompiledScenario(source=scenario.name, actions=[None]),
    )

    result = runner.invoke(app, ["compile-set", str(manifest)])

    assert result.exit_code == 0
    assert "wszystkie warianty aktualne" in result.output


def test_compile_set_success_wires_dependencies_and_closes_browser(tmp_path, monkeypatch):
    manifest, scenario = _write_render_set(tmp_path)
    browser, launches = _install_fake_playwright(monkeypatch)
    reasoner = object()
    calls = []

    monkeypatch.setattr(cli_module, "compile_up_to_date", lambda path: False)
    monkeypatch.setattr(cli_module, "CodexReasoner", lambda: reasoner)

    async def compile_set(plan, received_browser, received_reasoner, **kwargs):
        calls.append((plan, received_browser, received_reasoner, kwargs))
        return CompileSetResult(compiled=("en-US",), reused=())

    monkeypatch.setattr(cli_module, "run_compile_set", compile_set)

    result = runner.invoke(
        app,
        [
            "compile-set",
            str(manifest),
            "--headed",
            "--pause-on-error",
            "--timeout",
            "8.5",
            "--verbose",
        ],
    )

    assert result.exit_code == 0
    assert launches == [False]
    assert len(calls) == 1
    plan, received_browser, received_reasoner, kwargs = calls[0]
    assert [variant.scenario for variant in plan.variants] == [scenario]
    assert received_browser is browser
    assert received_reasoner is reasoner
    assert kwargs == {
        "timeout": 8.5,
        "force": False,
        "pause_on_error": True,
        "verbose": True,
    }
    assert browser.closed is True
    assert "skompilowano warianty: en-US" in result.output


def test_render_set_success_wires_dependencies_and_closes_browser(tmp_path, monkeypatch):
    manifest, scenario = _write_render_set(tmp_path)
    out_dir = tmp_path / "out"
    browser, launches = _install_fake_playwright(monkeypatch)
    provider = object()
    checked = []
    calls = []

    monkeypatch.setattr(cli_module, "ensure_render_set_compiled", checked.append)
    monkeypatch.setattr(cli_module, "EdgeTtsProvider", lambda: provider)

    async def render_set(
        plan,
        received_out_dir,
        received_provider,
        cache_dir,
        received_browser,
        **kwargs,
    ):
        calls.append(
            (
                plan,
                received_out_dir,
                received_provider,
                cache_dir,
                received_browser,
                kwargs,
            )
        )
        return [out_dir / "en.mp4"]

    monkeypatch.setattr(cli_module, "run_render_set", render_set)

    result = runner.invoke(
        app,
        [
            "render-set",
            str(manifest),
            "--output-dir",
            str(out_dir),
            "--headed",
            "--pause-on-error",
            "--timeout",
            "9.5",
            "--verbose",
        ],
    )

    assert result.exit_code == 0
    assert launches == [False]
    assert len(checked) == 1
    assert len(calls) == 1
    plan, received_out_dir, received_provider, cache_dir, received_browser, kwargs = calls[0]
    assert checked == [plan]
    assert [variant.scenario for variant in plan.variants] == [scenario]
    assert received_out_dir == out_dir
    assert received_provider is provider
    assert cache_dir == Path(".guidebot/audio")
    assert received_browser is browser
    reasoner = kwargs.pop("reasoner")
    assert kwargs == {"timeout": 9.5, "pause_on_error": True, "verbose": True}
    # Render sets heal pending optional branches too, so they get a reasoner on
    # the same terms as a plain render: only when the Codex CLI is installed.
    assert (reasoner is not None) == (shutil.which("codex") is not None)
    assert browser.closed is True
    assert f"zrenderowano: {out_dir / 'en.mp4'}" in result.output
