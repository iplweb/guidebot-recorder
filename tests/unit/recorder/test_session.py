"""Unit tests for the pre-recording session cache + orchestration (Phase A).

Pure logic only — no browser. The browser-driven pieces (``replay_setup``,
``check_logged_in``) are exercised in ``tests/integration/test_session_*.py``;
here they are monkeypatched so ``ensure_session``'s decision table can be
asserted deterministically.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guidebot_recorder.models.config import config_hash
from guidebot_recorder.recorder import session as session_mod
from guidebot_recorder.recorder.session import (
    RaisingReasoner,
    SetupNeedsCompile,
    SetupSessionError,
    ensure_session,
    load_session,
    save_session,
    session_cache_key,
)
from guidebot_recorder.scenario.loader import load_scenario

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

_TARGET_TEMPLATE = """\
config:
  title: Target
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  baseUrl: {base_url}
  setup: {setup_name}
steps:
  - navigate: "/"
"""


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
    path.write_text(
        _SETUP_TEMPLATE.format(base_url=base_url, extra=extra), encoding="utf-8"
    )
    return path


def _write_target(
    tmp_path: Path,
    *,
    name: str = "target.scenario.yaml",
    base_url: str = "https://example.com",
    setup_name: str = "setup.scenario.yaml",
) -> Path:
    path = tmp_path / name
    path.write_text(
        _TARGET_TEMPLATE.format(base_url=base_url, setup_name=setup_name),
        encoding="utf-8",
    )
    return path


#: env that satisfies the ${LOGIN_TOKEN}/${PASSWORD} references in the templates
_ENV = {"LOGIN_TOKEN": "tok", "PASSWORD": "pw"}


# --------------------------------------------------------------------------- #
# session_cache_key
# --------------------------------------------------------------------------- #


def test_cache_key_changes_with_referenced_env_value(tmp_path: Path) -> None:
    setup = _write_setup(tmp_path)
    cfg = load_scenario(setup, {"LOGIN_TOKEN": "t", "PASSWORD": "a"}).config

    env_a = {"LOGIN_TOKEN": "t", "PASSWORD": "secret-a"}
    env_b = {"LOGIN_TOKEN": "t", "PASSWORD": "secret-b"}
    key_a = session_cache_key(setup, cfg, env_a)
    key_b = session_cache_key(setup, cfg, env_b)
    assert key_a != key_b, "changing a referenced credential must change the key"


def test_cache_key_stable_across_unrelated_env(tmp_path: Path) -> None:
    setup = _write_setup(tmp_path)
    cfg = load_scenario(setup, {"LOGIN_TOKEN": "t", "PASSWORD": "a"}).config

    env_a = {"LOGIN_TOKEN": "t", "PASSWORD": "a", "UNRELATED": "1"}
    env_b = {"LOGIN_TOKEN": "t", "PASSWORD": "a", "UNRELATED": "2", "OTHER": "x"}
    assert session_cache_key(setup, cfg, env_a) == session_cache_key(setup, cfg, env_b)


def test_cache_key_never_embeds_raw_credential(tmp_path: Path) -> None:
    setup = _write_setup(tmp_path)
    cfg = load_scenario(setup, {"LOGIN_TOKEN": "t", "PASSWORD": "a"}).config
    key = session_cache_key(setup, cfg, {"LOGIN_TOKEN": "t", "PASSWORD": "hunter2"})
    assert "hunter2" not in key


def test_cache_key_changes_with_setup_config_hash(tmp_path: Path) -> None:
    setup = _write_setup(tmp_path)
    env = {"LOGIN_TOKEN": "t", "PASSWORD": "a"}
    cfg = load_scenario(setup, env).config
    key1 = session_cache_key(setup, cfg, env)

    # A different viewport → different config_hash → different key.
    other = _write_setup(tmp_path, name="setup2.scenario.yaml")
    other.write_text(
        other.read_text(encoding="utf-8").replace(
            "width: 800, height: 600", "width: 1024, height: 768"
        ),
        encoding="utf-8",
    )
    cfg2 = load_scenario(other, env).config
    assert config_hash(cfg) != config_hash(cfg2)
    # Same path resolve, different config_hash → key differs.
    key2 = session_cache_key(setup, cfg2, env)
    assert key1 != key2


# --------------------------------------------------------------------------- #
# save_session / load_session wrapper
# --------------------------------------------------------------------------- #

_STATE = {"cookies": [{"name": "s", "value": "1"}], "origins": []}


def test_wrapper_round_trip(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    path = save_session(sessions, "abc", _STATE, {"v": 1})
    assert path.exists()
    loaded = load_session(sessions, "abc", None)
    assert loaded == _STATE


def test_load_session_missing_returns_none(tmp_path: Path) -> None:
    assert load_session(tmp_path / "sessions", "nope", None) is None


def test_load_session_within_age(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, "k", _STATE, {})
    assert load_session(sessions, "k", 12) == _STATE


def test_load_session_past_max_age_returns_none(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, "k", _STATE, {})
    # Rewrite created_at to two hours ago.
    fpath = sessions / "k.json"
    data = json.loads(fpath.read_text(encoding="utf-8"))
    old = datetime.now(UTC) - timedelta(hours=2)
    data["created_at"] = old.isoformat()
    fpath.write_text(json.dumps(data), encoding="utf-8")

    assert load_session(sessions, "k", 1) is None
    # No TTL → still returned regardless of age.
    assert load_session(sessions, "k", None) == _STATE


def test_save_session_permissions_and_gitignore(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, "k", _STATE, {"v": 1})

    fpath = sessions / "k.json"
    assert stat.S_IMODE(fpath.stat().st_mode) == 0o600
    assert stat.S_IMODE(sessions.stat().st_mode) == 0o700

    gitignore = sessions / ".gitignore"
    assert gitignore.exists()
    assert gitignore.read_text(encoding="utf-8") == "*\n"

    # No leftover temp files from the atomic write.
    leftovers = [p.name for p in sessions.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_save_session_atomic_no_partial_file(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, "k", _STATE, {})
    # The final file is complete, valid JSON with the wrapper shape.
    data = json.loads((sessions / "k.json").read_text(encoding="utf-8"))
    assert set(data) == {"created_at", "key", "key_inputs", "storage_state"}
    assert data["storage_state"] == _STATE


# --------------------------------------------------------------------------- #
# RaisingReasoner
# --------------------------------------------------------------------------- #


async def test_raising_reasoner_raises_setup_needs_compile() -> None:
    with pytest.raises(SetupNeedsCompile):
        await RaisingReasoner().resolve("kliknij coś", [])


async def test_raising_reasoner_message_says_compile() -> None:
    with pytest.raises(SetupNeedsCompile, match="compile"):
        await RaisingReasoner().resolve("kliknij coś", [])


# --------------------------------------------------------------------------- #
# ensure_session — guards
# --------------------------------------------------------------------------- #


async def test_recursion_guard_raises(tmp_path: Path) -> None:
    # setup source that itself declares config.setup → error
    _write_setup(tmp_path, name="inner.scenario.yaml", verify=None, max_age=None)
    _write_setup(
        tmp_path,
        name="setup.scenario.yaml",
        nested_setup="inner.scenario.yaml",
    )
    target = _write_target(tmp_path)
    with pytest.raises(SetupSessionError):
        await ensure_session(
            None, target, tmp_path / "sessions", _ENV, timeout=5, warn=lambda m: None
        )


async def test_host_mismatch_raises(tmp_path: Path) -> None:
    _write_setup(tmp_path, base_url="https://auth.example.com")
    target = _write_target(tmp_path, base_url="https://app.other.com")
    with pytest.raises(SetupSessionError):
        await ensure_session(
            None, target, tmp_path / "sessions", _ENV, timeout=5, warn=lambda m: None
        )


# --------------------------------------------------------------------------- #
# ensure_session — decision table (monkeypatched replay/check/load/save)
# --------------------------------------------------------------------------- #


class _Recorder:
    def __init__(self) -> None:
        self.replayed = 0
        self.saved = 0
        self.checks: list[dict] = []
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _patch(monkeypatch, rec: _Recorder, *, cached, check_returns, replay_state):
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


async def test_present_and_verify_pass_reuses_without_replay(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path)
    target = _write_target(tmp_path)
    rec = _Recorder()
    cached = {"cookies": [{"name": "c"}], "origins": []}
    _patch(monkeypatch, rec, cached=cached, check_returns=True, replay_state={})

    out = await ensure_session(
        object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert out == cached
    assert rec.replayed == 0
    assert rec.saved == 0
    assert len(rec.checks) == 1


async def test_present_and_verify_fail_replays(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path)
    target = _write_target(tmp_path)
    rec = _Recorder()
    cached = {"cookies": [{"name": "old"}], "origins": []}
    fresh = {"cookies": [{"name": "new"}], "origins": []}
    # First check (on cached) fails; the post-replay check passes.
    checks = iter([False, True])

    async def fake_check(browser, storage_state, **kwargs):
        rec.checks.append(kwargs)
        return next(checks)

    async def fake_replay(browser, setup_path, env, *, timeout):
        rec.replayed += 1
        return fresh

    def fake_load(sessions_dir, key, max_age_hours):
        return cached

    def fake_save(sessions_dir, key, storage_state, key_inputs):
        rec.saved += 1
        return sessions_dir / f"{key}.json"

    monkeypatch.setattr(session_mod, "replay_setup", fake_replay)
    monkeypatch.setattr(session_mod, "check_logged_in", fake_check)
    monkeypatch.setattr(session_mod, "load_session", fake_load)
    monkeypatch.setattr(session_mod, "save_session", fake_save)

    out = await ensure_session(
        object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert out == fresh
    assert rec.replayed == 1
    assert rec.saved == 1


async def test_absent_replays(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path)
    target = _write_target(tmp_path)
    rec = _Recorder()
    fresh = {"cookies": [{"name": "new"}], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=True, replay_state=fresh)

    out = await ensure_session(
        object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert out == fresh
    assert rec.replayed == 1
    assert rec.saved == 1


async def test_no_verify_present_reuses_and_warns(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path, verify=None, max_age=None)
    target = _write_target(tmp_path)
    rec = _Recorder()
    cached = {"cookies": [{"name": "c"}], "origins": []}
    _patch(monkeypatch, rec, cached=cached, check_returns=True, replay_state={})

    out = await ensure_session(
        object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert out == cached
    assert rec.replayed == 0
    # verify is None → no health-check performed at all.
    assert rec.checks == []
    # loud warning emitted (no verify + no maxAge).
    assert rec.warnings, "expected a loud warning when no health-check is configured"


async def test_no_verify_no_maxage_warns_on_replay(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path, verify=None, max_age=None)
    target = _write_target(tmp_path)
    rec = _Recorder()
    fresh = {"cookies": [{"name": "new"}], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=True, replay_state=fresh)

    out = await ensure_session(
        object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
    )
    assert out == fresh
    assert rec.replayed == 1
    assert rec.checks == []
    assert rec.warnings


async def test_replay_then_verify_fail_raises_empty_state_diagnostic(
    tmp_path, monkeypatch
) -> None:
    _write_setup(tmp_path)
    target = _write_target(tmp_path)
    rec = _Recorder()
    empty = {"cookies": [], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=False, replay_state=empty)

    with pytest.raises(SetupSessionError, match="sessionStorage|IndexedDB|outside"):
        await ensure_session(
            object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
        )
    assert rec.replayed == 1
    assert rec.saved == 1


async def test_replay_then_verify_fail_raises_text_diagnostic(tmp_path, monkeypatch) -> None:
    _write_setup(tmp_path)
    target = _write_target(tmp_path)
    rec = _Recorder()
    nonempty = {"cookies": [{"name": "c", "value": "v"}], "origins": []}
    _patch(monkeypatch, rec, cached=None, check_returns=False, replay_state=nonempty)

    with pytest.raises(SetupSessionError, match="verifyUserLoggedIn|--headed|not found"):
        await ensure_session(
            object(), target, tmp_path / "s", _ENV, timeout=5, warn=rec.warn
        )
    assert rec.replayed == 1
