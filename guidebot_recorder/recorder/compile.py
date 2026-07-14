"""Faza `compile` — algorytm §5.6.

Uruchamia scenariusz sekwencyjnie na świeżej sesji, dla kroków z namiarem woła
Reasonera (tylko gdy brak ważnego cache), waliduje, zamraża `cachedAction` w tym
samym pliku. LLM zwraca wyłącznie dane; akcje wykonuje Playwright.

Viewport ustawiany jest z `config` — MUSI zgadzać się z fazą render, inaczej
zamrożone pozycje elementów nie pasują (element „outside of the viewport”).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.models.action import ActionKind, CachedAction, Expect, Fingerprint
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
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.scenario.roundtrip import atomic_write, inject_cached_action

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
        return resolved  # click / hover — wnioskowany przez LLM
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
    """Krótki, czytelny opis kroku do logu verbose."""
    for attr in ("say", "teach", "navigate", "click", "hover"):
        value = getattr(step, attr)
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


async def run_compile(
    path: Path | str,
    page: Page,
    reasoner: Reasoner,
    env: Mapping[str, str] | None = None,
    *,
    force: bool = False,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> None:
    path = Path(path)
    loaded = load_scenario(path, env)
    scenario = loaded.scenario
    cfg = scenario.config
    chash = config_hash(cfg)
    # KLUCZOWE: ten sam viewport co render, inaczej zamrożone pozycje nie pasują.
    await page.set_viewport_size({"width": cfg.viewport.width, "height": cfg.viewport.height})
    recorder = Recorder(page, overlay=None)

    steps = scenario.steps
    bar = tqdm(total=len(steps), desc="compile", unit="krok", disable=not verbose)
    try:
        for index, step in enumerate(steps):
            kind = step.command_kind()
            if verbose:
                tqdm.write(f"[{index + 1}/{len(steps)}] {kind}: {_short(step)}")
            try:
                await _compile_step(
                    page,
                    recorder,
                    scenario,
                    loaded,
                    path,
                    chash,
                    index,
                    step,
                    kind,
                    reasoner,
                    force=force,
                    verbose=verbose,
                )
            except Exception as exc:
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {exc}")
                if pause_on_error:
                    await pause_for_inspection(page, "compile", index, kind, exc)
                raise
            bar.update(1)
    finally:
        bar.close()


async def _compile_step(
    page: Page,
    recorder: Recorder,
    scenario: Scenario,
    loaded,
    path: Path,
    chash: str,
    index: int,
    step: Step,
    kind: str,
    reasoner: Reasoner,
    *,
    force: bool,
    verbose: bool,
) -> None:
    if kind == "say":
        return
    if kind == "navigate":
        await recorder.navigate(_resolve_url(scenario, step.navigate))
        return
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return

    # krok wymagający namiaru
    cached = step.cached_action
    if not force and cached is not None and await reuse_is_valid(page, cached):
        action, target, state, expect = cached.action, cached.target, cached.state, cached.expect
        identity = cached.identity
        fresh = False
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
        # tożsamość zamrażamy PRZED wykonaniem akcji; waitFor:hidden jej nie ma
        if action == "waitFor" and state == "hidden":
            identity = None
        else:
            identity = await capture_identity(locator)
        fresh = True
        expect = None
        if verbose:
            tqdm.write(f"   ↳ {action} → {_target_desc(target)}")

    # wykonaj akcję (odsłania stan dla kolejnych kroków)
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
        action_model = CachedAction(
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
        inject_cached_action(loaded.doc, index, action_model)
        atomic_write(path, loaded.doc)

    await recorder.apply_readiness(expect)
