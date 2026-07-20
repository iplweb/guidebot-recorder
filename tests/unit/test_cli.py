import shutil
import textwrap
from pathlib import Path

from typer.testing import CliRunner

import guidebot_recorder.cli as cli_module
from guidebot_recorder.cli import app
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.recorder.render_set import CompileSetResult
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled

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
