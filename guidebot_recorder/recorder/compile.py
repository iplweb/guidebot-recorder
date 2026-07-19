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

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from tqdm import tqdm

from guidebot_recorder.models.action import (
    COMPILER_VERSION,
    ActionKind,
    CachedAction,
    Expect,
    Fingerprint,
    validate_teach_input_text,
    validate_teach_instruction,
)
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import config_hash, site_viewport
from guidebot_recorder.models.scenario import Scenario, Step, WaitUntil
from guidebot_recorder.models.target import (
    LabelTarget,
    RoleTarget,
    Target,
    TestidTarget,
    TextTarget,
)
from guidebot_recorder.recorder._debug import (
    pause_for_inspection,
    redact_exception,
    redact_text,
    scenario_sensitive_values,
)
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.identity_capture import capture_identity
from guidebot_recorder.resolver.page_context import collect_candidates
from guidebot_recorder.resolver.reasoner import Reasoner, ReasonerError, ReasonerResult
from guidebot_recorder.resolver.validate import (
    ValidationOk,
    is_sensitive_type_target,
    reuse_is_valid,
    validate_compile_time,
)
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references

_MAX_REPROMPT = 2
_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1


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
        return resolved  # click / hover / type — inferred by the LLM
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
    if step.slide is not None:
        return step.slide.title or step.slide.subtitle or "slide"
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
    if prior.compiler_version != COMPILER_VERSION:
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
    path = Path(path)
    scenario = load_scenario(path, env)
    chash = config_hash(scenario.config)
    cpath = compiled_path(path)
    if not _compiled_artifact_is_current(cpath, path.name, len(scenario.steps)):
        return False
    actions = _load_prior_actions(cpath, len(scenario.steps))
    if any(
        not step.requires_target() and actions[index] is not None
        for index, step in enumerate(scenario.steps)
    ):
        return False
    return not _steps_needing_resolution(scenario, actions, chash, force)


def _compiled_artifact_is_current(cpath: Path, source_name: str, n_steps: int) -> bool:
    """Validate artifact-level invariants, including targetless scenarios."""

    try:
        compiled = load_compiled(cpath)
    except Exception:  # noqa: BLE001 — missing/corrupt artifacts simply need compile
        return False
    return (
        compiled.compiler_version == COMPILER_VERSION
        and compiled.source == source_name
        and len(compiled.actions) == n_steps
        and all(
            action is None or action.fingerprint.compiler_version == COMPILER_VERSION
            for action in compiled.actions
        )
    )


async def run_compile_in_browser(
    path: Path | str,
    browser: Browser,
    reasoner: Reasoner,
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 30.0,
    force: bool = False,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> None:
    """Compile in a fresh context matching the scenario's render locale.

    A localized page can expose different labels from ``navigator.language`` or
    ``Accept-Language``. Creating the context here prevents compile from freezing
    targets against a different DOM than render will later replay.
    """

    scenario = load_scenario(path, env)
    cfg = scenario.config
    # When chrome is enabled the site renders inside the shell iframe of height
    # ``H - chrome.height``; compile must resolve it at the same reduced viewport.
    site_width, site_height = site_viewport(cfg)
    context = await browser.new_context(
        viewport={"width": site_width, "height": site_height},
        locale=cfg.locale,
    )
    try:
        page = await context.new_page()
        await run_compile(
            path,
            page,
            reasoner,
            env,
            timeout=timeout,
            force=force,
            pause_on_error=pause_on_error,
            verbose=verbose,
        )
    finally:
        await context.close()


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
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cfg = scenario.config
    chash = config_hash(cfg)
    # CRUCIAL: the same viewport as render, otherwise frozen positions do not match.
    # With chrome enabled that is the shell iframe interior (``H - chrome.height``).
    site_width, site_height = site_viewport(cfg)
    await page.set_viewport_size({"width": site_width, "height": site_height})
    page.set_default_timeout(timeout * 1000)
    main_page = page
    context = page.context
    active_page = main_page
    popup_page: Page | None = None
    popup_seen = False
    loop = asyncio.get_running_loop()
    observed_pages: list[Page] = [main_page]
    page_opened_at: dict[Page, float] = {main_page: loop.time()}

    def observe_page(candidate: Page) -> None:
        if all(candidate is not observed for observed in observed_pages):
            observed_pages.append(candidate)
            page_opened_at[candidate] = loop.time()

    context.on("page", observe_page)

    steps = scenario.steps
    cpath = compiled_path(path)
    actions = _load_prior_actions(cpath, len(steps))

    bar = tqdm(total=len(steps), desc="compile", unit="krok", disable=not verbose)
    try:
        for index, step in enumerate(steps):
            if main_page.is_closed():
                raise RuntimeError("główne okno zostało zamknięte podczas compile")
            if active_page.is_closed():
                raise RuntimeError("popup zamknął się poza obsługiwaną akcją scenariusza")
            if _unexpected_pages(observed_pages, main_page, popup_page):
                raise RuntimeError(f"krok {index}: nieoczekiwany popup poza akcją click")
            active_page.set_default_timeout(timeout * 1000)
            await active_page.bring_to_front()
            recorder = Recorder(active_page, overlay=None)
            kind = step.command_kind()
            if kind == "teach":
                try:
                    validate_teach_instruction(_instruction(step))
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
            if verbose:
                description = redact_text(_short(step), sensitive_values)
                tqdm.write(f"[{index + 1}/{len(steps)}] {kind}: {description}")
            try:
                pages_before = tuple(context.pages)
                observed_start = len(observed_pages)
                click_observed_start: int | None = None
                click_started_at: float | None = None

                def arm_click_observation(
                    step_observed_start: int = observed_start,
                ) -> None:
                    """Assign only pages created after this point to the click."""

                    nonlocal click_observed_start, click_started_at
                    if observed_pages[step_observed_start:]:
                        raise RuntimeError(
                            "nieoczekiwany popup otworzył się podczas rozwiązywania "
                            "kroku, przed akcją click"
                        )
                    click_observed_start = len(observed_pages)
                    click_started_at = loop.time()

                action_page = active_page
                compiled_action = await _compile_step(
                    active_page,
                    recorder,
                    scenario,
                    chash,
                    index,
                    step,
                    kind,
                    reasoner,
                    actions[index],
                    before_click=arm_click_observation,
                    force=force,
                    verbose=verbose,
                )
                action_page_closed_in_window = action_page.is_closed()

                new_pages: list[Page] = []
                if compiled_action is not None and compiled_action.action == "click":
                    if (
                        click_observed_start is None or click_started_at is None
                    ):  # pragma: no cover - dispatch invariant
                        raise RuntimeError("wewnętrzny błąd obserwacji akcji click")
                    new_pages = await _wait_for_new_pages(
                        context,
                        pages_before,
                        observed_pages,
                        click_observed_start,
                        page_opened_at,
                        started_at=click_started_at,
                    )
                elif observed_pages[observed_start:]:
                    raise RuntimeError("popup może zostać otwarty tylko przez akcję click")
                if len(new_pages) > 1:
                    raise RuntimeError("v1 obsługuje dokładnie jeden popup w sesji")
                if new_pages:
                    if popup_seen:
                        raise RuntimeError("v1 obsługuje co najwyżej jeden popup w całej sesji")
                    openers = [await candidate.opener() for candidate in new_pages]
                    if any(opener is not active_page for opener in openers):
                        raise RuntimeError("nowa strona nie jest popupem aktywnego okna")
                    popup_seen = True
                    popup = new_pages[0]
                    popup_page = popup
                    compiled_action = compiled_action.model_copy(update={"opens_popup": True})
                    popup.set_default_timeout(timeout * 1000)
                    prepared = await _prepare_popup(
                        popup,
                        {"width": cfg.viewport.width, "height": cfg.viewport.height},
                    )
                    if not prepared:
                        raise RuntimeError("popup zamknął się podczas otwierania")
                    active_page = popup
                elif compiled_action is not None and compiled_action.opens_popup:
                    # Refresh observed lifecycle metadata even when the target/action
                    # itself was safely reused.
                    compiled_action = compiled_action.model_copy(update={"opens_popup": False})
                if main_page.is_closed():
                    raise RuntimeError("główne okno zostało zamknięte podczas compile")
                if active_page.is_closed():
                    close_was_action_driven = (
                        active_page is action_page
                        and action_page_closed_in_window
                        and compiled_action is not None
                        and compiled_action.action in {"click", "hover", "type"}
                    )
                    if not close_was_action_driven:
                        raise RuntimeError(
                            "popup zamknął się asynchronicznie poza obsługiwaną akcją"
                        )
                    active_page = main_page
                    await active_page.bring_to_front()
                    await Recorder(active_page, overlay=None).apply_readiness("none")
                    await active_page.wait_for_load_state()
                if _unexpected_pages(observed_pages, main_page, popup_page):
                    raise RuntimeError(f"krok {index}: nieoczekiwany dodatkowy popup")
                actions[index] = compiled_action
            except Exception as exc:
                safe_message = redact_exception(exc, sensitive_values)
                if verbose:
                    tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
                if pause_on_error:
                    debug_page = active_page if not active_page.is_closed() else main_page
                    await pause_for_inspection(
                        debug_page,
                        "compile",
                        index,
                        kind,
                        exc,
                        sensitive_values,
                    )
                raise RuntimeError(f"{type(exc).__name__}: {safe_message}") from None
            # persist incrementally so partial progress survives a later failure
            write_compiled(cpath, CompiledScenario(source=path.name, actions=actions))
            bar.update(1)
    finally:
        context.remove_listener("page", observe_page)
        bar.close()


def _can_reuse(cached_in: CachedAction | None, step: Step, chash: str, force: bool) -> bool:
    """Reuse only if the frozen fingerprint still matches the source and config."""
    if force or cached_in is None:
        return False
    fp = cached_in.fingerprint
    expected_state = step.wait.state if isinstance(step.wait, WaitUntil) else None
    return (
        fp.compiler_version == COMPILER_VERSION
        and fp.command_kind == step.command_kind()
        and fp.compiled_from == _instruction(step)
        and fp.config_hash == chash
        and fp.state == expected_state
        and fp.expect == cached_in.expect
    )


def _new_pages(context: BrowserContext, known: tuple[Page, ...]) -> list[Page]:
    return [
        candidate for candidate in context.pages if all(candidate is not page for page in known)
    ]


def _unexpected_pages(
    observed_pages: list[Page], main_page: Page, popup_page: Page | None
) -> list[Page]:
    """Observed pages outside the main + one-popup session contract."""

    return [
        candidate
        for candidate in observed_pages
        if candidate is not main_page and candidate is not popup_page
    ]


async def _wait_for_new_pages(
    context: BrowserContext,
    known: tuple[Page, ...],
    observed: list[Page] | None = None,
    observed_start: int = 0,
    opened_at: Mapping[Page, float] | None = None,
    *,
    started_at: float | None = None,
    timeout: float = _POPUP_DETECTION_SECONDS,
) -> list[Page]:
    """Find pages opened inside the bounded window of the actual click."""

    loop = asyncio.get_running_loop()
    started_at = loop.time() if started_at is None else started_at
    deadline = started_at + timeout
    cutoff = deadline
    first_seen_at: float | None = None
    while True:
        found: list[Page] = []
        candidates = list((observed or [])[observed_start:]) + _new_pages(context, known)
        for candidate in candidates:
            candidate_opened_at = (opened_at or {}).get(candidate, loop.time())
            if started_at <= candidate_opened_at <= deadline and all(
                candidate is not page for page in found
            ):
                found.append(candidate)
        if found:
            if first_seen_at is None:
                first_seen_at = min((opened_at or {}).get(page, loop.time()) for page in found)
                cutoff = max(cutoff, first_seen_at + _POPUP_QUIESCENCE_SECONDS)
            if loop.time() - first_seen_at >= _POPUP_QUIESCENCE_SECONDS:
                return found
        remaining = cutoff - loop.time()
        if remaining <= 0:
            return found
        await asyncio.sleep(min(0.05, remaining))


async def _prepare_popup(page: Page, viewport: dict[str, int]) -> bool:
    """Apply page policy; return false only when the page closed mid-prepare."""

    if page.is_closed():
        return False
    try:
        await page.set_viewport_size(viewport)
        await page.bring_to_front()
        await page.wait_for_load_state()
    except PlaywrightError:
        if page.is_closed():
            return False
        raise
    return not page.is_closed()


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
    before_click: Callable[[], None],
    force: bool,
    verbose: bool,
) -> CachedAction | None:
    if kind == "say":
        return None
    if kind == "slide":
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
        input_text = cached_in.input_text
        if verbose:
            tqdm.write("   ↳ reuse (cache)")
    else:
        instruction = _instruction(step)
        candidates = await collect_candidates(page)
        resolved = None
        resolution_error: str | None = None
        for _ in range(_MAX_REPROMPT):
            result = await reasoner.resolve(instruction, candidates)
            if isinstance(result, ReasonerError):
                raise RuntimeError(f"reasoner: {result.reason}: {result.message}")
            assert isinstance(result, ReasonerResult)
            action = _action_for(kind, result.action)
            input_text = result.input_text if action == "type" and kind == "teach" else None
            if action == "type" and kind == "teach":
                if not isinstance(input_text, str):
                    resolution_error = (
                        "reasoner nie zwrócił niepustego inputText dla akcji teach → type"
                    )
                    continue
                try:
                    validate_teach_input_text(instruction, input_text)
                except ValueError as exc:
                    resolution_error = str(exc)
                    continue
            resolution_error = None
            validation = await validate_compile_time(page, result.target, action)
            if isinstance(validation, ValidationOk):
                if (
                    action == "type"
                    and kind == "teach"
                    and await is_sensitive_type_target(validation.locator)
                ):
                    resolution_error = (
                        "pole wygląda na przeznaczone dla wartości wrażliwej; użyj enterText z ENV"
                    )
                    continue
                resolved = (action, result.target, validation.locator, input_text)
                break
        if resolved is None:
            if resolution_error is not None:
                raise RuntimeError(f"{resolution_error} po {_MAX_REPROMPT} próbach")
            raise RuntimeError(f"nie udało się zwalidować namiaru dla: {instruction!r}")
        action, target, locator, input_text = resolved
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
        try:
            await recorder.click(target, before_click=before_click)
        except PlaywrightError:
            # A "close" button whose onclick calls ``window.close()`` can race the
            # click and raise TargetClosedError once the page tears down. If the
            # click closed the page, its intent succeeded — swallow it and let the
            # caller observe the now-closed page (mirrors the apply_readiness
            # tolerance below). Any other click failure (page still open) raises.
            if not page.is_closed():
                raise
    elif action == "hover":
        await recorder.hover(target)
    elif action == "type":
        text = step.enter_text.text if step.enter_text is not None else input_text
        if text is None:
            raise RuntimeError("brak tekstu dla akcji type")
        await recorder.enter_text(target, text)
    elif action == "waitFor":
        timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        await recorder.wait_for(target, state or "visible", timeout)
    url_after = page.url if not page.is_closed() else url_before

    if fresh:
        expect = heuristic_expect(url_before, url_after)
        cached_out = CachedAction(
            action=action,
            target=target,
            identity=identity,
            expect=expect,
            state=state,
            input_text=input_text,
            fingerprint=Fingerprint(
                command_kind=kind,
                compiled_from=_instruction(step),
                expect=expect,
                config_hash=chash,
                state=state,
            ),
        )

    if not page.is_closed():
        try:
            await recorder.apply_readiness(expect)
        except PlaywrightError:
            if not page.is_closed():
                raise
    return cached_out
