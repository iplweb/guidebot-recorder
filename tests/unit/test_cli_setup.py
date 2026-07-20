"""CLI tests for `guidebot setup` — the setup-session establishment command.

``establish_session`` is monkeypatched (no browser); we only assert the command
wiring: outcome echo on success, and exit-1 + stderr message on a fatal error.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

import guidebot_recorder.cli as cli_module
from guidebot_recorder.cli import app
from guidebot_recorder.recorder.session import SetupNeedsCompile

runner = CliRunner()

_SETUP = textwrap.dedent(
    """\
    config:
      title: Setup
      viewport: {width: 640, height: 480}
      tts: {provider: edge, voice: v, lang: pl-PL}
      baseUrl: https://example.com
      verifyUserLoggedIn: "Wyloguj"
    steps:
      - navigate: "/login"
    """
)


def _install_fake_playwright(monkeypatch):
    class FakeBrowser:
        closed = False

        async def close(self):
            self.closed = True

    browser = FakeBrowser()

    class FakeChromium:
        async def launch(self, *, headless):
            return browser

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightManager:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(cli_module, "async_playwright", FakePlaywrightManager)
    return browser


def _write_setup(tmp_path: Path) -> Path:
    path = tmp_path / "setup.scenario.yaml"
    path.write_text(_SETUP, encoding="utf-8")
    return path


def test_setup_reports_refreshed_and_exits_zero(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    _install_fake_playwright(monkeypatch)
    calls = []

    async def fake_establish(browser, scenario, sessions_dir, env, **kwargs):
        calls.append((scenario, sessions_dir, kwargs))
        return ("refreshed", {})

    monkeypatch.setattr(cli_module, "establish_session", fake_establish)

    result = runner.invoke(app, ["setup", str(setup)])

    assert result.exit_code == 0
    assert "session refreshed and cached" in result.output
    assert len(calls) == 1
    _scenario, sessions_dir, kwargs = calls[0]
    assert sessions_dir == Path(".guidebot/sessions")
    assert kwargs["force"] is False
    assert kwargs["manual"] is False


def test_setup_reports_reused(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    _install_fake_playwright(monkeypatch)

    async def fake_establish(browser, scenario, sessions_dir, env, **kwargs):
        return ("reused", {})

    monkeypatch.setattr(cli_module, "establish_session", fake_establish)

    result = runner.invoke(app, ["setup", str(setup)])

    assert result.exit_code == 0
    assert "session reused (already live)" in result.output


def test_setup_headed_and_force_flags_are_threaded(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    _install_fake_playwright(monkeypatch)
    captured = {}

    async def fake_establish(browser, scenario, sessions_dir, env, **kwargs):
        captured.update(kwargs)
        return ("refreshed", {})

    monkeypatch.setattr(cli_module, "establish_session", fake_establish)

    result = runner.invoke(app, ["setup", str(setup), "--headed", "--force"])

    assert result.exit_code == 0
    assert captured["force"] is True
    # --headed enables manual completion of the login.
    assert captured["manual"] is True


def test_setup_reports_error_on_needs_compile(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    _install_fake_playwright(monkeypatch)

    async def fake_establish(browser, scenario, sessions_dir, env, **kwargs):
        raise SetupNeedsCompile("run `guidebot compile <setup>` first")

    monkeypatch.setattr(cli_module, "establish_session", fake_establish)

    result = runner.invoke(app, ["setup", str(setup)])

    assert result.exit_code == 1
    assert "guidebot compile" in result.output
