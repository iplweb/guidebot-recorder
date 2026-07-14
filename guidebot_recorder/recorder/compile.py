"""Faza `compile` — algorytm §5.6.

Uruchamia scenariusz sekwencyjnie na świeżej sesji, dla kroków z namiarem woła
Reasonera (tylko gdy brak ważnego cache), waliduje, zamraża `cachedAction` w tym
samym pliku. LLM zwraca wyłącznie dane; akcje wykonuje Playwright.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin

from playwright.async_api import Page

from guidebot_recorder.models.action import ActionKind, CachedAction, Expect, Fingerprint
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
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


async def run_compile(
    path: Path | str, page: Page, reasoner: Reasoner, env: Mapping[str, str] | None = None
) -> None:
    path = Path(path)
    loaded = load_scenario(path, env)
    scenario = loaded.scenario
    chash = config_hash(scenario.config)
    recorder = Recorder(page, overlay=None)

    for index, step in enumerate(scenario.steps):
        kind = step.command_kind()

        if kind == "say":
            continue
        if kind == "navigate":
            await recorder.navigate(_resolve_url(scenario, step.navigate))
            continue
        if kind == "wait" and not step.requires_target():
            await recorder.wait_seconds(float(step.wait))
            continue

        # krok wymagający namiaru
        cached = step.cached_action
        if cached is not None and await reuse_is_valid(page, cached):
            action, target, state, expect = (
                cached.action,
                cached.target,
                cached.state,
                cached.expect,
            )
            identity = cached.identity
            fresh = False
        else:
            instruction = _instruction(step)
            candidates = await collect_candidates(page)
            resolved = None
            for _ in range(_MAX_REPROMPT):
                result = await reasoner.resolve(instruction, candidates)
                if isinstance(result, ReasonerError):
                    raise RuntimeError(
                        f"compile: krok {index}: reasoner: {result.reason}: {result.message}"
                    )
                assert isinstance(result, ReasonerResult)
                action = _action_for(kind, result.action)
                validation = await validate_compile_time(page, result.target, action)
                if isinstance(validation, ValidationOk):
                    resolved = (action, result.target, validation.locator)
                    break
            if resolved is None:
                raise RuntimeError(
                    f"compile: krok {index}: nie udało się zwalidować: {instruction!r}"
                )
            action, target, locator = resolved
            state = step.wait.state if isinstance(step.wait, WaitUntil) else None
            # tożsamość zamrażamy PRZED wykonaniem akcji (DOM może się zmienić);
            # waitFor:hidden nie ma tożsamości do porównania
            identity = None if (action == "waitFor" and state == "hidden") else await capture_identity(locator)
            fresh = True
            expect = None  # wyznaczymy po wykonaniu akcji

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
