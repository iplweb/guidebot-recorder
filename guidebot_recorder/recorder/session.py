"""Pre-recording setup — cached session, health-check, replay orchestration.

Phase A of the "pre-recording setup" feature (see
``docs/superpowers/specs/2026-07-20-pre-recording-setup-design.md``). This module
establishes a prepared, cached browser session (Playwright ``storage_state``) by
replaying an already-compiled *setup* scenario through the existing compile path
with a reasoner that always raises, then validates it with a cheap text
health-check and caches it under ``.guidebot/sessions/``.

Security invariants held here:

- The session file is a bearer credential: ``0o600`` file inside a ``0o700``
  directory, plus a self-writing ``.gitignore`` (``*``). Written atomically.
- Raw credential values NEVER appear anywhere except folded inside the cache-key
  hash (via ``env_digest``). No page text (``document.innerText``) is ever logged
  or placed in any raised message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from guidebot_recorder.models.config import Config, config_hash, site_viewport
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references

__all__ = [
    "RaisingReasoner",
    "SetupNeedsCompile",
    "SetupSessionError",
    "check_logged_in",
    "ensure_session",
    "load_session",
    "replay_setup",
    "save_session",
    "session_cache_key",
]

_KEY_VERSION = 1

# JS that resolves true once the (possibly async SPA) body renders the text.
_CONTAINS_TEXT = "t => !!document.body && document.body.innerText.includes(t)"


class SetupNeedsCompile(RuntimeError):
    """The setup scenario is not (fully) compiled: an LLM resolve was required.

    Raised by :class:`RaisingReasoner` during replay and surfaced by
    :func:`replay_setup`. The user must run ``guidebot compile <setup>`` first.
    """


class SetupSessionError(RuntimeError):
    """A setup session could not be established or reused (fatal, non-recoverable)."""


class RaisingReasoner:
    """A :class:`~guidebot_recorder.resolver.reasoner.Reasoner` that never infers.

    Replaying a *fully compiled* setup sidecar drives the page purely from frozen
    targets and never asks the reasoner anything. If a target is not reuse-valid
    (missing/stale) the compile path falls back to ``resolve`` — which here fails
    loudly, telling the user to compile the setup first. This keeps setup/render
    at ZERO LLM calls. The instruction and candidate snapshot are untrusted page
    context, so they are deliberately excluded from the message.
    """

    async def resolve(
        self, instruction: str, candidates: list[Candidate]
    ) -> ReasonerResult | ReasonerError:
        raise SetupNeedsCompile(
            "the setup scenario is not fully compiled — run "
            "`guidebot compile <setup>` before establishing a session"
        )


# --------------------------------------------------------------------------- #
# Cache key
# --------------------------------------------------------------------------- #


def _env_digest(setup_path: Path | str, env: Mapping[str, str] | None) -> str:
    """Digest of the referenced ``${ENV}`` (name, value) pairs.

    Folding the values into a hash (never storing them raw) means changing the
    login user/password abandons the old session, without ever exposing a
    low-entropy credential to offline guessing from a filename.
    """

    refs = scenario_env_references(setup_path, env)
    payload = json.dumps(sorted(refs.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_inputs(
    setup_path: Path | str, setup_cfg: Config, env: Mapping[str, str] | None
) -> dict[str, object]:
    """The non-secret canonical projection the cache key is derived from."""

    return {
        "v": _KEY_VERSION,
        "setup": str(Path(setup_path).resolve()),
        "baseUrl": setup_cfg.base_url,
        "config_hash": config_hash(setup_cfg),
        "env_digest": _env_digest(setup_path, env),
    }


def session_cache_key(
    setup_path: Path | str, setup_cfg: Config, env: Mapping[str, str] | None
) -> str:
    """SHA-256 over the canonical, credential-free projection of the setup source."""

    payload = json.dumps(
        _key_inputs(setup_path, setup_cfg, env), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Cache file (wrapper): save / load
# --------------------------------------------------------------------------- #


def _ensure_sessions_dir(sessions_dir: Path) -> None:
    """Create the sessions dir (``0o700``) and its self-writing ``.gitignore``."""

    sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A pre-existing dir may have looser perms (e.g. created by an older tool).
    try:
        os.chmod(sessions_dir, 0o700)
    except OSError:  # pragma: no cover - best-effort on exotic filesystems
        pass
    gitignore = sessions_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


def save_session(
    sessions_dir: Path,
    key: str,
    storage_state: dict,
    key_inputs: dict,
) -> Path:
    """Persist ``storage_state`` (wrapped) atomically as ``<key>.json`` (``0o600``)."""

    sessions_dir = Path(sessions_dir)
    _ensure_sessions_dir(sessions_dir)
    wrapper = {
        "created_at": datetime.now(UTC).isoformat(),
        "key": key,
        "key_inputs": key_inputs,
        "storage_state": storage_state,
    }
    target = sessions_dir / f"{key}.json"
    tmp = sessions_dir / f".{key}.json.tmp"
    # Write with 0o600 from the start so the credential is never briefly readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(wrapper, fh, ensure_ascii=False)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return target


def load_session(sessions_dir: Path, key: str, max_age_hours: float | None) -> dict | None:
    """Return the cached ``storage_state`` dict, or None if missing/expired.

    Age is computed from the wrapper's ``created_at`` (UTC), never file mtime, so
    the TTL survives ``git clean``, copies, and CI restore.
    """

    fpath = Path(sessions_dir) / f"{key}.json"
    if not fpath.exists():
        return None
    try:
        wrapper = json.loads(fpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(wrapper, dict) or "storage_state" not in wrapper:
        return None
    if max_age_hours is not None:
        created_raw = wrapper.get("created_at")
        if not isinstance(created_raw, str):
            return None
        try:
            created = datetime.fromisoformat(created_raw)
        except ValueError:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - created).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            return None
    state = wrapper["storage_state"]
    return state if isinstance(state, dict) else None


# --------------------------------------------------------------------------- #
# Replay the setup scenario → storage_state
# --------------------------------------------------------------------------- #


async def replay_setup(
    browser: Browser,
    setup_path: Path,
    env: Mapping[str, str] | None,
    *,
    timeout: float,
) -> dict:
    """Replay the compiled setup scenario on a non-recording context; snapshot it.

    Drives the setup scenario purely from its frozen targets (via
    :class:`RaisingReasoner`) — inheriting pop-up handling, pending gates,
    readiness waits, URL resolution, ``${ENV}`` substitution and redaction for
    free — then captures ``storage_state`` BEFORE closing the context.
    """

    setup_path = Path(setup_path)
    cfg = load_scenario(setup_path, env).config
    site_width, site_height = site_viewport(cfg)
    context = await browser.new_context(
        viewport={"width": site_width, "height": site_height},
        locale=cfg.locale,
    )
    try:
        page = await context.new_page()
        try:
            await run_compile(
                setup_path,
                page,
                RaisingReasoner(),
                env,
                timeout=timeout,
                force=False,
            )
        except SetupNeedsCompile:
            raise
        except RuntimeError as exc:
            # run_compile catches the reasoner's SetupNeedsCompile and re-raises a
            # plain RuntimeError whose message embeds the type name (with `from
            # None`, so the original is not chained). Detect that and surface a
            # clean, actionable SetupNeedsCompile instead of a generic failure.
            if "SetupNeedsCompile" in str(exc):
                raise SetupNeedsCompile(
                    "the setup scenario is not fully compiled — compile the setup "
                    "scenario first with `guidebot compile <setup>`"
                ) from None
            raise
        state = await context.storage_state()
    finally:
        await context.close()
    return state


# --------------------------------------------------------------------------- #
# Health-check
# --------------------------------------------------------------------------- #


async def check_logged_in(
    browser: Browser,
    storage_state: dict,
    *,
    goto_url: str,
    contains_text: str,
    locale: str | None,
    viewport: dict[str, int],
    timeout: float,
) -> bool:
    """True if ``contains_text`` renders on ``goto_url`` under ``storage_state``.

    Polls ``document.body.innerText.includes(<contains_text>)`` (avoiding false
    negatives on async SPA shells). Never logs or returns any page text: only the
    boolean verdict leaves this function.
    """

    context = await browser.new_context(
        storage_state=storage_state,
        locale=locale,
        viewport=viewport,
    )
    try:
        page = await context.new_page()
        await page.goto(goto_url)
        try:
            await page.wait_for_function(
                _CONTAINS_TEXT, arg=contains_text, timeout=timeout * 1000
            )
        except PlaywrightTimeoutError:
            return False
        return True
    finally:
        await context.close()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _hosts_differ(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return urlsplit(a).hostname != urlsplit(b).hostname


def _health_url(target_cfg: Config, setup_cfg: Config, verify) -> str | None:
    """Origin the health-check visits: TARGET base_url preferred (cookies are
    origin-scoped), with ``verify.url`` overriding the path against that origin."""

    origin = target_cfg.base_url or setup_cfg.base_url
    if verify is not None and verify.url:
        return urljoin(origin or "", verify.url)
    return origin


async def ensure_session(
    browser: Browser,
    target_scenario_path: Path,
    sessions_dir: Path,
    env: Mapping[str, str] | None,
    *,
    timeout: float,
    warn: Callable[[str], None] = logging.warning,
) -> dict:
    """Return a live prepared ``storage_state`` for ``target``'s setup scenario.

    Reuses a cached session when a configured health-check confirms it is still
    live; otherwise replays the setup scenario (un-recorded), caches the result,
    and re-checks. Fails loudly, with a disambiguated message, if a configured
    health-check still fails after a fresh replay.
    """

    target_scenario_path = Path(target_scenario_path)
    target = load_scenario(target_scenario_path, env)
    rel = target.config.setup
    if rel is None:
        raise SetupSessionError(
            "target scenario has no config.setup — ensure_session must not be called"
        )
    setup_path = (target_scenario_path.parent / rel).resolve()
    setup = load_scenario(setup_path, env)

    # Recursion guard: a setup source must not itself declare config.setup.
    if setup.config.setup is not None:
        raise SetupSessionError(
            "a setup scenario must not itself declare config.setup (no nested setup)"
        )

    # Host guard: cross-origin session reuse is not sound in v1.
    if _hosts_differ(target.config.base_url, setup.config.base_url):
        raise SetupSessionError(
            "setup and target base_url hosts differ — cross-origin session reuse "
            "is not supported"
        )

    verify = setup.config.verify_user_logged_in
    max_age = setup.config.max_age_hours

    if verify is None and max_age is None:
        warn(
            "pre-recording setup: neither verifyUserLoggedIn nor maxAgeHours is "
            "configured on the setup scenario — a present cached session will be "
            "trusted (never re-checked) until you pass --force"
        )

    key = session_cache_key(setup_path, setup.config, env)
    key_inputs = _key_inputs(setup_path, setup.config, env)
    goto_url = _health_url(target.config, setup.config, verify)

    def _check_kwargs(state: dict) -> dict:
        assert verify is not None  # only called when a health-check is configured
        return {
            "goto_url": goto_url,
            "contains_text": verify.contains_text,
            "locale": setup.config.locale,
            "viewport": dict(
                zip(("width", "height"), site_viewport(setup.config), strict=True)
            ),
            "timeout": verify.timeout,
        }

    cached = load_session(sessions_dir, key, max_age)
    if cached is not None:
        if verify is None:
            return cached
        if await check_logged_in(browser, cached, **_check_kwargs(cached)):
            return cached

    storage_state = await replay_setup(browser, setup_path, env, timeout=timeout)
    save_session(sessions_dir, key, storage_state, key_inputs)

    if verify is not None and not await check_logged_in(
        browser, storage_state, **_check_kwargs(storage_state)
    ):
        if not _has_persisted_state(storage_state):
            raise SetupSessionError(
                "setup ran but produced no cookies or localStorage: this app may "
                "keep its session outside cookies/localStorage "
                "(sessionStorage/IndexedDB) — pre-recording setup cannot cache it"
            )
        raise SetupSessionError(
            "setup ran and a session was cached, but the logged-in text was not "
            "found: check verifyUserLoggedIn, or complete login manually with "
            "`guidebot setup <setup> --headed`"
        )

    return storage_state


def _has_persisted_state(storage_state: dict) -> bool:
    """Whether the snapshot carries any cookies or localStorage origins."""

    cookies = storage_state.get("cookies") or []
    origins = storage_state.get("origins") or []
    return bool(cookies) or bool(origins)
