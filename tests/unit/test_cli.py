"""Testy CLI dla poleceń jednoscenariuszowych: `validate`, `compile`, `render`,
`setup`, `guide`.

Wszystkie biorą na wejściu jeden plik scenariusza. Polecenia zestawów
(`compile-set`, `render-set`), które wchodzą przez `_load_set_or_exit`
i manifest `localized-render-set`, mieszkają w `test_cli_sets.py`; `setup`
ma dodatkowo własny plik `test_cli_setup.py` (ustanawianie sesji).

Wspólne z `test_cli_sets.py` są tylko cztery rzeczy — zepsuty scenariusz,
asercja bannera, atrapa Playwrighta i zamrożony sidecar pozycyjny — i przychodzą
jawnym importem z `_cli_helpers`.
"""

import textwrap

from typer.testing import CliRunner

import guidebot_recorder.cli as cli_module
from guidebot_recorder.cli import app

from ._cli_helpers import (
    BAD_TWO_COMMANDS,
    _assert_validation_banner,
    _freeze_positional_sidecar,
    _install_fake_playwright,
)

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


POSITIONAL = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 640, height: 480}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - click: "Usuń"
    """
)


def test_compile_opens_the_browser_for_a_frozen_positional_index(tmp_path, monkeypatch):
    """Zamrożony `nth` jest wart tyle, co strona, na której go zmierzono.

    Odcisk kroku nie zmienia się od przebudowy strony, więc bramka „nic do
    skompilowania" jest jedynym miejscem, w którym wykrywanie dryfu może umrzeć.
    """

    path = tmp_path / "positional.scenario.yaml"
    path.write_text(POSITIONAL, encoding="utf-8")
    _freeze_positional_sidecar(path)
    browser, launches = _install_fake_playwright(monkeypatch)
    calls = []

    async def compile_in_browser(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(cli_module, "CodexReasoner", lambda: object())
    monkeypatch.setattr(cli_module, "run_compile_in_browser", compile_in_browser)

    result = runner.invoke(app, ["compile", str(path)])

    assert result.exit_code == 0
    assert "nic do skompilowania" not in result.output
    assert launches == [True]
    assert len(calls) == 1
    assert browser.closed is True
