"""Unit tests for the setup-scenario-driven ``establish_session`` orchestrator.

``establish_session`` is the CLI-facing counterpart to ``ensure_session``: it is
driven by the *setup* scenario itself (not a target that references one). Pure
logic only — the browser-touching helpers (``replay_setup``, ``check_logged_in``,
``load_session``, ``save_session``, ``_manual_finish``) are monkeypatched so the
reuse/refresh/manual decision table is asserted deterministically with no browser.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from guidebot_recorder.recorder import session as session_mod
from guidebot_recorder.recorder.session import (
    SetupSessionError,
    establish_session,
)

# --------------------------------------------------------------------------- #
# Scenario-file helpers
# --------------------------------------------------------------------------- #

_SETUP_TEMPLATE = """\
config:
  title: Setup
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
{extra}steps:
  - navigate: "/login?token=${{LOGIN_TOKEN}}"
  - enterText: {{into: "hasło", text: "${{PASSWORD}}"}}
"""

#: env that satisfies the ${LOGIN_TOKEN}/${PASSWORD} references in the template
_ENV = {"LOGIN_TOKEN": "tok", "PASSWORD": "pw"}


def _write_setup(
    tmp_path: Path,
    *,
    name: str = "setup.scenario.yaml",
    base_url: str = "https://example.com",
    verify: str | None = "Wyloguj",
    max_age: float | None = 12,
    nested_setup: str | None = None,
) -> Path:
    extra = ""
    if verify is not None:
        extra += f'  verifyUserLoggedIn: "{verify}"\n'
    if max_age is not None:
        extra += f"  maxAgeHours: {max_age}\n"
    if nested_setup is not None:
        extra += f"  setup: {nested_setup}\n"
    path = tmp_path / name
    path.write_text(_SETUP_TEMPLATE.format(base_url=base_url, extra=extra), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Patch harness
# --------------------------------------------------------------------------- #


class _Recorder:
    def __init__(self) -> None:
        self.replayed = 0
        self.saved = 0
        self.checks: list[dict] = []
        self.warnings: list[str] = []
        self.manual_calls = 0
        self.prompts: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return ""


def _patch(
    monkeypatch,
    rec: _Recorder,
    *,
    cached,
    check_returns,
    replay_state,
):
    async def fake_replay(browser, setup_path, env, *, timeout):
        rec.replayed += 1
        return replay_state

    async def fake_check(browser, storage_state, **kwargs):
        rec.checks.append(kwargs)
        return check_returns

    def fake_load(sessions_dir, key, max_age_hours):
        return cached

    def fake_save(sessions_dir, key, storage_state, key_inputs):
        rec.saved += 1
        return sessions_dir / f"{key}.json"

    monkeypatch.setattr(session_mod, "replay_setup", fake_replay)
    monkeypatch.setattr(session_mod, "check_logged_in", fake_check)
    monkeypatch.setattr(session_mod, "load_session", fake_load)
    monkeypatch.setattr(session_mod, "save_session", fake_save)


# --------------------------------------------------------------------------- #
# recursion guard
# --------------------------------------------------------------------------- #


async def test_nested_setup_raises(tmp_path: Path) -> None:
    _write_setup(tmp_path, name="inner.scenario.yaml", verify=None, max_age=None)
    setup = _write_setup(tmp_path, nested_setup="inner.scenario.yaml")
    with pytest.raises(SetupSessionError, match="nested setup"):
        await establish_session(
            object(), setup, tmp_path / "s", _ENV, timeout=5, warn=lambda m: None
        )


# --------------------------------------------------------------------------- #
# decision table
# --------------------------------------------------------------------------- #


async def test_reused_when_cache_present_and_check_passes(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    cached = {"cookies": [{"name": "c"}], "origins": []}
    _patch(monkeypatch, rec, cached=cached, check_returns=True, replay_state={})

    status, state = await establish_session(
        object(), setup, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert status == "reused"
    assert state == cached
    assert rec.replayed == 0
    assert rec.saved == 0
    assert len(rec.checks) == 1


async def test_force_bypasses_live_cache_and_replays(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    cached = {"cookies": [{"name": "old"}], "origins": []}
    fresh = {"cookies": [{"name": "new"}], "origins": []}
    # cache would satisfy the check, but --force must ignore it entirely.
    _patch(monkeypatch, rec, cached=cached, check_returns=True, replay_state=fresh)

    status, state = await establish_session(
        object(), setup, tmp_path / "s", _ENV, timeout=5, force=True, warn=rec.warn
    )
    assert status == "refreshed"
    assert state == fresh
    assert rec.replayed == 1
    assert rec.saved == 1
    # No check against the cache (it was never consulted); only the post-replay one.
    assert len(rec.checks) == 1


async def test_refreshed_when_cache_absent(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    fresh = {"cookies": [{"name": "new"}], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=True, replay_state=fresh)

    status, state = await establish_session(
        object(), setup, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert status == "refreshed"
    assert state == fresh
    assert rec.replayed == 1
    assert rec.saved == 1


async def test_verify_fail_empty_state_raises_diagnostic(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    empty = {"cookies": [], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=False, replay_state=empty)

    with pytest.raises(SetupSessionError, match="sessionStorage|IndexedDB|outside"):
        await establish_session(
            object(), setup, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
        )
    assert rec.replayed == 1
    assert rec.saved == 1


async def test_verify_fail_text_not_found_raises_diagnostic(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    nonempty = {"cookies": [{"name": "c", "value": "v"}], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=False, replay_state=nonempty)

    with pytest.raises(SetupSessionError, match="verifyUserLoggedIn|--headed|not found"):
        await establish_session(
            object(), setup, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
        )
    assert rec.replayed == 1


async def test_manual_finish_then_recheck_passes(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    replayed = {"cookies": [{"name": "c", "value": "v"}], "origins": []}
    manual_state = {"cookies": [{"name": "c", "value": "v"}, {"name": "sso"}], "origins": []}
    # Post-replay check fails; after manual finish the re-check passes.
    checks = iter([False, True])

    async def fake_check(browser, storage_state, **kwargs):
        rec.checks.append(kwargs)
        return next(checks)

    async def fake_replay(browser, setup_path, env, *, timeout):
        rec.replayed += 1
        return replayed

    def fake_load(sessions_dir, key, max_age_hours):
        return None

    def fake_save(sessions_dir, key, storage_state, key_inputs):
        rec.saved += 1
        return sessions_dir / f"{key}.json"

    async def fake_manual(browser, setup_cfg, goto_url, storage_state, prompt):
        rec.manual_calls += 1
        prompt("Finish logging in in the browser window, then press Enter...")
        return manual_state

    monkeypatch.setattr(session_mod, "replay_setup", fake_replay)
    monkeypatch.setattr(session_mod, "check_logged_in", fake_check)
    monkeypatch.setattr(session_mod, "load_session", fake_load)
    monkeypatch.setattr(session_mod, "save_session", fake_save)
    monkeypatch.setattr(session_mod, "_manual_finish", fake_manual)

    status, state = await establish_session(
        object(),
        setup,
        tmp_path / "s",
        _ENV,
        timeout=5,
        manual=True,
        prompt=rec.prompt,
        warn=rec.warn,
    )
    assert status == "refreshed"
    assert state == manual_state
    assert rec.manual_calls == 1
    assert rec.prompts  # prompt was invoked
    # replay-check (fail) + manual re-check (pass)
    assert len(rec.checks) == 2
    # replay save + manual save
    assert rec.saved == 2


async def test_manual_finish_still_failing_raises(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path)
    rec = _Recorder()
    nonempty = {"cookies": [{"name": "c", "value": "v"}], "origins": []}
    # Check fails post-replay and again after the manual attempt.
    _patch(monkeypatch, rec, cached=None, check_returns=False, replay_state=nonempty)

    async def fake_manual(browser, setup_cfg, goto_url, storage_state, prompt):
        rec.manual_calls += 1
        return nonempty

    monkeypatch.setattr(session_mod, "_manual_finish", fake_manual)

    with pytest.raises(SetupSessionError, match="verifyUserLoggedIn|--headed|not found"):
        await establish_session(
            object(),
            setup,
            tmp_path / "s",
            _ENV,
            timeout=5,
            manual=True,
            prompt=rec.prompt,
            warn=rec.warn,
        )
    assert rec.manual_calls == 1


async def test_no_verify_no_maxage_warns(tmp_path, monkeypatch) -> None:
    setup = _write_setup(tmp_path, verify=None, max_age=None)
    rec = _Recorder()
    cached = {"cookies": [{"name": "c"}], "origins": []}
    _patch(monkeypatch, rec, cached=cached, check_returns=True, replay_state={})

    status, state = await establish_session(
        object(), setup, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert status == "reused"
    assert state == cached
    # no verify → no health-check, cache trusted
    assert rec.checks == []
    assert rec.replayed == 0
    assert rec.warnings, "expected a loud warning when no health-check is configured"


# --------------------------------------------------------------------------- #
# _manual_finish drives the browser and returns the re-snapshotted state
# --------------------------------------------------------------------------- #


async def test_manual_finish_drives_browser_and_snapshots(tmp_path) -> None:
    setup = _write_setup(tmp_path)
    from guidebot_recorder.scenario.loader import load_scenario

    cfg = load_scenario(setup, _ENV).config
    new_state = {"cookies": [{"name": "after-manual"}], "origins": []}

    page = MagicMock()
    page.goto = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock(return_value=new_state)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)

    prompts: list[str] = []

    result = await session_mod._manual_finish(
        browser, cfg, cfg.base_url, {"cookies": [], "origins": []}, prompts.append
    )
    assert result == new_state
    assert prompts, "prompt must be shown to the operator"
    page.goto.assert_awaited_once_with(cfg.base_url)
    context.close.assert_awaited_once()
