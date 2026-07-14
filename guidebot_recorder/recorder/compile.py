"""The `compile` phase — algorithm §5.6.

Runs the scenario sequentially on a fresh session; for steps that need a target it
calls the Reasoner (only when there is no valid cache), validates, and freezes the
``cachedAction``. The LLM only returns data; Playwright performs the actions.

The source scenario is read-only: resolved actions are written to a separate
``*.compiled.yaml`` (a list aligned by index to the steps).

The viewport is taken from ``config`` — it MUST match the render phase, otherwise
the frozen element positions do not line up ("element outside of the viewport").
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.models.action import ActionKind, CachedAction, Expect, Fingerprint
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    Target,
    TestidTarget,
    TextTarget,
)
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import collect_candidates
from guidebot_recorder.resolver.reasoner import Reasoner, ReasonerError, ReasonerResult
from guidebot_recorder.resolver.validate import (
    ValidationOk,
    reuse_is_valid,
    validate_compile_time,
)
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario

_MAX_REPROMPT = 2


def _instruction(step: Step) -> str:
    kind = step.command_kind()
    if kind == "teach":
        return step.teach
    if kind == "click":
        return step.click
    if kind == "hover":
        return step.hover
    if kind == "enterText":
        return step.enter_text.into
    if kind == "wait":
        return step.wait.until
    raise ValueError(f"krok bez instrukcji do rozwiązania: {kind}")


def _action_for(kind: str, resolved: ActionKind) -> ActionKind:
    if kind == "teach":
        return resolved  # click / hover — inferred by the LLM
    if kind == "click":
        return "click"
    if kind == "hover":
        return "hover"
    if kind == "enterText":
        return "type"
    if kind == "wait":
        return "waitFor"
    raise ValueError(f"krok bez akcji: {kind}")


def heuristic_expect(url_before: str, url_after: str) -> Expect:
    return "navigation" if url_before != url_after else "none"


def _resolve_url(scenario: Scenario, url: str) -> str:
    base = scenario.config.base_url
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


def _short(step: Step, limit: int = 60) -> str:
    """Short, readable step description for the verbose log."""
    for attr in ("say", "teach", "navigate", "click", "hover"):
        value = step.navigate_url() if attr == "navigate" else getattr(step, attr)
        if value:
            text = str(value)
            return text if len(text) <= limit else text[: limit - 1] + "…"
    if step.enter_text is not None:
        return f"→ {step.enter_text.into}"
    if step.wait is not None:
        return step.wait.until if isinstance(step.wait, WaitUntil) else f"{step.wait}s"
    return ""


def _target_desc(target: Target) -> str:
    if isinstance(target, RoleTarget):
        return f'role={target.role} name="{target.name}"'
    if isinstance(target, TextTarget):
        return f'text="{target.text}"'
    if isinstance(target, LabelTarget):
        return f'label="{target.label}"'
    if isinstance(target, TestidTarget):
        return f"testid={target.testid}"
    return str(target)


def _load_prior_actions(cpath: Path, n_steps: int) -> list[CachedAction | None]:
    """Load existing compiled actions for reuse, aligned by index to the current steps.

    Steps appended at the end stay ``None`` (to be resolved). If a step is inserted
    or removed mid-scenario the indices shift and the per-step fingerprint check
    (:func:`_can_reuse`) will simply re-resolve the affected steps — correctness is
    never traded for the incremental speed-up.
    """
    result: list[CachedAction | None] = [None] * n_steps
    if not cpath.exists():
        return result
    try:
        prior = load_compiled(cpath)
    except Exception:  # noqa: BLE001 — a corrupt/stale compiled file just means recompile
        return result
    for i in range(min(len(prior.actions), n_steps)):
        result[i] = prior.actions[i]
    return result


def _steps_needing_resolution(
    scenario: Scenario, actions: list[CachedAction | None], chash: str, force: bool
) -> list[int]:
    """Indices of target steps whose frozen action is missing or stale."""
    return [
        i
        for i, step in enumerate(scenario.steps)
        if step.requires_target() and not _can_reuse(actions[i], step, chash, force)
    ]


def compile_up_to_date(
    path: Path | str, env: Mapping[str, str] | None = None, *, force: bool = False
) -> bool:
    """True if every target step already has a valid frozen action — no browser needed.

    Lets the CLI skip launching Chromium entirely when the only edits were to
    non-target steps (e.g. ``say`` narration) or nothing at all.
    """
    if force:
        return False
    scenario = load_scenario(path, env)
    chash = config_hash(scenario.config)
    actions = _load_prior_actions(compiled_path(Path(path)), len(scenario.steps))
    return not _steps_needing_resolution(scenario, actions, chash, force)


async def run_compile(
    path: Path | str,
    page: Page,
    reasoner: Reasoner,
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 30.0,
    force: bool = False,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> None:
    path = Path(path)
    scenario = load_scenario(path, env)
    cfg = scenario.config
    chash = config_hash(cfg)
    # CRUCIAL: the same viewport as render, otherwise frozen positions do not match.
    await page.set_viewport_size({"width": cfg.viewport.width, "height": cfg.viewport.height})
    page.set_default_timeout(timeout * 1000)
    recorder = Recorder(page, overlay=None)

    steps = scenario.steps
    cpath = compiled_path(path)
    actions = _load_prior_actions(cpath, len(steps))

    bar = tqdm(total=len(steps), desc="compile", unit="krok", disable=not verbose)
    try:
        for index, step in enumerate(steps):
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(steps)}] {kind}: {_short(step)}")
            try:
                actions[index] = await _compile_step(
                    page,
                    recorder,
                    scenario,
                    chash,
                    index,
                    step,
                    kind,
                    reasoner,
                    actions[index],
                    force=force,
                    verbose=verbose,
                )
            except Exception as exc:
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {exc}")
                if pause_on_error:
                    await pause_for_inspection(page, "compile", index, kind, exc)
                raise
            # persist incrementally so partial progress survives a later failure
            write_compiled(cpath, CompiledScenario(source=path.name, actions=actions))
            bar.update(1)
    finally:
        bar.close()


def _can_reuse(cached_in: CachedAction | None, step: Step, chash: str, force: bool) -> bool:
    """Reuse only if the frozen fingerprint still matches the source and config."""
    if force or cached_in is None:
        return False
    fp = cached_in.fingerprint
    return fp.compiled_from == _instruction(step) and fp.config_hash == chash


async def _compile_step(
    page: Page,
    recorder: Recorder,
    scenario: Scenario,
    chash: str,
    index: int,
    step: Step,
    kind: str,
    reasoner: Reasoner,
    cached_in: CachedAction | None,
    *,
    force: bool,
    verbose: bool,
) -> CachedAction | None:
    if kind == "say":
        return None
    if kind == "navigate":
        url = step.navigate_url()
        assert url is not None  # guaranteed by command_kind()
        await recorder.navigate(_resolve_url(scenario, url))
        return None
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return None

    # step that needs a target
    if _can_reuse(cached_in, step, chash, force) and await reuse_is_valid(page, cached_in):
        action, target, state, expect = (
            cached_in.action,
            cached_in.target,
            cached_in.state,
            cached_in.expect,
        )
        cached_out = cached_in
        fresh = False
        identity = cached_in.identity
        if verbose:
            tqdm.write("   ↳ reuse (cache)")
    else:
        instruction = _instruction(step)
        candidates = await collect_candidates(page)
        resolved = None
        for _ in range(_MAX_REPROMPT):
            result = await reasoner.resolve(instruction, candidates)
            if isinstance(result, ReasonerError):
                raise RuntimeError(f"reasoner: {result.reason}: {result.message}")
            assert isinstance(result, ReasonerResult)
            action = _action_for(kind, result.action)
            validation = await validate_compile_time(page, result.target, action)
            if isinstance(validation, ValidationOk):
                resolved = (action, result.target, validation.locator)
                break
        if resolved is None:
            raise RuntimeError(f"nie udało się zwalidować namiaru dla: {instruction!r}")
        action, target, locator = resolved
        state = step.wait.state if isinstance(step.wait, WaitUntil) else None
        # freeze identity BEFORE the action (the DOM may change); waitFor:hidden has none
        if action == "waitFor" and state == "hidden":
            identity = None
        else:
            identity = await capture_identity(locator)
        fresh = True
        expect = None
        cached_out = None  # built after the action, once we know `expect`
        if verbose:
            tqdm.write(f"   ↳ {action} → {_target_desc(target)}")

    # perform the action (reveals the state for later steps)
    url_before = page.url
    if action == "click":
        await recorder.click(target)
    elif action == "hover":
        await recorder.hover(target)
    elif action == "type":
        await recorder.enter_text(target, step.enter_text.text)
    elif action == "waitFor":
        timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        await recorder.wait_for(target, state or "visible", timeout)
    url_after = page.url

    if fresh:
        expect = heuristic_expect(url_before, url_after)
        cached_out = CachedAction(
            action=action,
            target=target,
            identity=identity,
            expect=expect,
            state=state,
            fingerprint=Fingerprint(
                command_kind=kind,
                compiled_from=_instruction(step),
                expect=expect,
                config_hash=chash,
                state=state,
            ),
        )

    await recorder.apply_readiness(expect)
    return cached_out
