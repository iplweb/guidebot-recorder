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
from collections.abc import Callable, Iterable, Mapping
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
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import (
    COMPILER_VERSION,
    CachedAction,
    Fingerprint,
    PendingAction,
    validate_teach_instruction,
)
from guidebot_recorder.models.compiled import CompiledAction, CompiledScenario
from guidebot_recorder.models.config import config_hash, site_viewport
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
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
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    TargetAbsent,
    TargetResolutionError,
    compiled_from,
    heuristic_expect,
    resolve_step_target,
    step_state,
)
from guidebot_recorder.resolver.resolution import (
    step_instruction as _instruction,
)
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.scenario.source import ScenarioSource, StepLocation
from guidebot_recorder.selects import Selects, SelectsNotReadyError, install_selects

__all__ = [
    "compile_up_to_date",
    "heuristic_expect",
    "run_compile",
    "run_compile_in_browser",
]

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1


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
    if step.close_window is not None:
        return "closeWindow"
    if step.slide is not None:
        return step.slide.title or step.slide.subtitle or "slide"
    if step.desktop is not None:
        return f"desktop: {step.desktop.icon}"
    if step.enter_text is not None:
        return f"→ {step.enter_text.into}"
    if step.highlight is not None:
        return f"◯ {step.highlight.what}"
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


def _load_prior_actions(cpath: Path, n_steps: int) -> list[CompiledAction | None]:
    """Load existing compiled actions for reuse, aligned by index to the current steps.

    Steps appended at the end stay ``None`` (to be resolved). If a step is inserted
    or removed mid-scenario the indices shift and the per-step fingerprint check
    (:func:`_can_reuse`) will simply re-resolve the affected steps — correctness is
    never traded for the incremental speed-up.
    """
    result: list[CompiledAction | None] = [None] * n_steps
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
    flat: list[FlatStep], actions: list[CompiledAction | None], chash: str, force: bool
) -> list[int]:
    """Flat indices of target steps whose frozen action is missing or stale."""
    return [
        i
        for i, entry in enumerate(flat)
        if entry.step.requires_target() and not _can_reuse(actions[i], entry.step, chash, force)
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
    flat = scenario.flat_steps()
    if not _compiled_artifact_is_current(cpath, path.name, len(flat)):
        return False
    actions = _load_prior_actions(cpath, len(flat))
    if any(
        not entry.step.requires_target() and actions[index] is not None
        for index, entry in enumerate(flat)
    ):
        return False
    return not _steps_needing_resolution(flat, actions, chash, force)


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
    # Pre-recording setup: when the target declares ``config.setup`` its login
    # steps were removed, so the compile context must resolve targets against the
    # already-logged-in DOM (spec: "Target compile", review §1). ``ensure_session``
    # is imported lazily: ``session`` imports ``run_compile`` from this module at
    # its top, so a module-level import here would form a cycle.
    setup_state = None
    if cfg.setup is not None:
        from guidebot_recorder.recorder.session import ensure_session

        setup_state = await ensure_session(
            browser, Path(path), Path(".guidebot/sessions"), env, timeout=timeout
        )
    context = await browser.new_context(
        viewport={"width": site_width, "height": site_height},
        locale=cfg.locale,
        **({"storage_state": setup_state} if setup_state is not None else {}),
    )
    try:
        # Registered before the first page exists, so every document compile ever
        # sees is already shimmed. Compile has no overlays of its own, but it must
        # resolve against the same DOM render drives — otherwise a `<select>` is
        # frozen as the native control and replayed as the widget.
        selects = await install_selects(context, cfg)
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
            selects=selects,
        )
    finally:
        await context.close()


async def run_compile(
    path: Path | str,
    page: Page,
    reasoner: Reasoner,
    env: Mapping[str, str] | None = None,
    *,
    selects: Selects | None,
    timeout: float = 30.0,
    force: bool = False,
    pause_on_error: bool = False,
    verbose: bool = False,
) -> None:
    """Compile the scenario on an already-prepared page.

    ``selects`` is the controller returned by
    :func:`guidebot_recorder.selects.install_selects` for this page's context,
    or ``None`` when no shim was installed (``mode: native``, or a caller
    driving a bare context). It is passed in rather than rebuilt here because
    only the caller that created the context knows whether the init script was
    actually registered — waiting on a widget that was never injected would fail
    every compile instead of catching a real problem.

    Required rather than defaulted, deliberately. A caller that forgot it would
    silently lose the readiness barrier and freeze targets against an unshimmed
    DOM that render then drives shimmed — a divergence nothing would report.
    Passing ``selects=None`` says "no shim here" out loud, at the call site.
    """

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

    # Flat indexing: a `when:` block contributes its synthetic gate step followed by
    # its children, so `actions` stays positionally 1:1 with what actually executes.
    flat = scenario.flat_steps()
    cpath = compiled_path(path)
    actions = _load_prior_actions(cpath, len(flat))
    artifact_dirty = not _compiled_artifact_is_current(cpath, path.name, len(flat))
    #: branch whose gate turned out to be absent — its children are recorded pending
    skipped_branch: int | None = None

    def checkpoint() -> None:
        """Persist only semantic progress, while keeping fresh resolves crash-safe."""

        nonlocal artifact_dirty
        write_compiled(cpath, CompiledScenario(source=path.name, actions=actions))
        artifact_dirty = False

    def banner(entry: FlatStep, entry_index: int, message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=entry_index,
            total=len(flat),
            location=entry.location,
            source=scenario.source,
            message=message,
            sensitive=sensitive_values,
        )

    bar = tqdm(total=len(flat), desc="compile", unit="krok", disable=not verbose)
    try:
        for index, entry in enumerate(flat):
            step = entry.step
            action_before = actions[index]
            if skipped_branch is not None and entry.branch == skipped_branch:
                # The gate never showed up: freeze the child as pending so a later
                # render can resolve it in place, and do not execute it now.
                actions[index] = _pending_for(step, chash) if step.requires_target() else None
                if actions[index] != action_before:
                    artifact_dirty = True
                    checkpoint()
                bar.update(1)
                continue
            skipped_branch = None
            if main_page.is_closed():
                raise RuntimeError("główne okno zostało zamknięte podczas compile")
            if active_page.is_closed():
                raise RuntimeError("popup zamknął się poza obsługiwaną akcją scenariusza")
            if _unexpected_pages(observed_pages, main_page, popup_page):
                raise RuntimeError(banner(entry, index, "nieoczekiwany popup poza akcją click"))
            active_page.set_default_timeout(timeout * 1000)
            await active_page.bring_to_front()
            recorder = Recorder(active_page, overlay=None)
            kind = step.command_kind()
            if kind == "closeWindow" and active_page is main_page:
                raise RuntimeError(banner(entry, index, "closeWindow bez otwartego okna"))
            if kind == "teach":
                try:
                    validate_teach_instruction(_instruction(step))
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
            if verbose:
                description = redact_text(_short(step), sensitive_values)
                tqdm.write(f"[{index + 1}/{len(flat)}] {kind}: {description}")
            try:
                if selects is not None and step.requires_target():
                    # Readiness barrier: the resolver's page snapshot must be
                    # taken against the shimmed DOM (the navigation that led here
                    # has settled by now — it was an earlier step). Without it
                    # compile can freeze a target the render, running with the
                    # widget in place, no longer recognises.
                    try:
                        await selects.wait_ready(active_page)
                    except SelectsNotReadyError as exc:
                        # A wedged widget is a step-level failure like any other,
                        # so it arrives with `plik:linia` and the YAML fragment
                        # rather than beside them. The two fixes the message
                        # names (`selects.settleMs`, `selects.mode: native`) are
                        # both edits to this very file.
                        raise RuntimeError(banner(entry, index, str(exc))) from exc
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
                    optional=entry.is_gate or step.optional,
                    entry=entry,
                    total=len(flat),
                    sensitive=sensitive_values,
                )
                action_page_closed_in_window = action_page.is_closed()

                new_pages: list[Page] = []
                if isinstance(compiled_action, CachedAction) and compiled_action.action == "click":
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
                elif isinstance(compiled_action, CachedAction) and compiled_action.opens_popup:
                    # Refresh observed lifecycle metadata even when the target/action
                    # itself was safely reused.
                    compiled_action = compiled_action.model_copy(update={"opens_popup": False})
                if main_page.is_closed():
                    raise RuntimeError("główne okno zostało zamknięte podczas compile")
                if active_page.is_closed():
                    close_was_action_driven = kind == "closeWindow" or (
                        active_page is action_page
                        and action_page_closed_in_window
                        and isinstance(compiled_action, CachedAction)
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
                    raise RuntimeError(banner(entry, index, "nieoczekiwany dodatkowy popup"))
                actions[index] = compiled_action
                if isinstance(compiled_action, PendingAction):
                    _warn_absent(
                        index,
                        step,
                        gate=entry.is_gate,
                        total=len(flat),
                        location=entry.location,
                        source=scenario.source,
                        sensitive=sensitive_values,
                    )
                    if entry.is_gate:
                        skipped_branch = entry.branch
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
                        total=len(flat),
                        location=entry.location,
                        source=scenario.source,
                    )
                raise RuntimeError(f"{type(exc).__name__}: {safe_message}") from None
            if actions[index] != action_before:
                # A fresh resolve (or refreshed popup lifecycle metadata) is real
                # progress and remains checkpointed before the next step. Pure
                # cache hits and targetless steps do not rewrite the full sidecar.
                artifact_dirty = True
                checkpoint()
            bar.update(1)
        if flat and artifact_dirty:
            # Targetless scenarios and artifact-only repairs still need one final,
            # aligned sidecar even though no per-step action changed.
            checkpoint()
    finally:
        context.remove_listener("page", observe_page)
        bar.close()


def _pending_for(step: Step, chash: str) -> PendingAction:
    """Placeholder for a target that was optional and absent at compile time.

    ``expect`` is only settled by actually performing the action, so the
    fingerprint carries the neutral ``"none"``; the entry exists to keep the
    usual version/config invalidation working until render resolves it.
    """

    return PendingAction(
        fingerprint=Fingerprint(
            command_kind=step.command_kind(),
            compiled_from=compiled_from(step),
            expect="none",
            config_hash=chash,
            state=step_state(step),
        )
    )


def _warn_absent(
    index: int,
    step: Step,
    *,
    gate: bool,
    total: int,
    location: StepLocation | None = None,
    source: ScenarioSource | None = None,
    sensitive: Iterable[str] = (),
) -> None:
    """Ostrzeż o nieobecnym elemencie opcjonalnym — banner z `plik:linia`.

    Instrukcja kroku bywa dosłowną kopią wartości wstrzykniętej przez `${ENV}`,
    więc `sensitive` nie jest ozdobnikiem: bez niego sekret wyciekłby wierszem
    pod „bezpiecznym" fragmentem YAML-a.
    """

    what = "element bramkujący" if gate else "element opcjonalny"
    tqdm.write(
        step_banner(
            index=index,
            total=total,
            location=location,
            source=source,
            message=(
                f"{what} {_instruction(step)!r} nie pojawił się — "
                "zapisano wpis oczekujący (pending); render rozwiąże go na miejscu"
            ),
            warning=True,
            sensitive=sensitive,
        )
    )


def _fingerprint_matches(fp: Fingerprint, step: Step, chash: str) -> bool:
    return (
        fp.compiler_version == COMPILER_VERSION
        and fp.command_kind == step.command_kind()
        and fp.compiled_from == compiled_from(step)
        and fp.config_hash == chash
        and fp.state == step_state(step)
    )


def _can_reuse(cached_in: CompiledAction | None, step: Step, chash: str, force: bool) -> bool:
    """Reuse only if the frozen fingerprint still matches the source and config.

    A :class:`PendingAction` counts as reusable on purpose: the element it stands
    for is optional, so retrying it would launch a browser and burn the full gate
    timeout on every compile for something that may never be there. ``--force``
    re-attempts. It has no ``expect`` to cross-check — that is only settled once
    the action actually runs.
    """
    if force or cached_in is None:
        return False
    if isinstance(cached_in, PendingAction):
        return _fingerprint_matches(cached_in.fingerprint, step, chash)
    return (
        _fingerprint_matches(cached_in.fingerprint, step, chash)
        and cached_in.fingerprint.expect == cached_in.expect
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
    cached_in: CompiledAction | None,
    *,
    before_click: Callable[[], None],
    force: bool,
    verbose: bool,
    optional: bool = False,
    entry: FlatStep | None = None,
    total: int = 0,
    sensitive: Iterable[str] = (),
) -> CompiledAction | None:
    """Resolve and perform one step, returning the action to freeze (or ``None``).

    ``entry`` (plus ``total`` and ``sensitive``) serves diagnostics only: error
    messages point at `plik:linia` and quote the YAML fragment. All three are
    keyword-only with defaults — the positional arguments are untouched, and
    without them the banner degrades to a bare step number, exactly as
    ``_render_step`` does on the render side.
    """

    def step_message(message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=index,
            total=total,
            location=entry.location if entry is not None else None,
            source=scenario.source,
            message=message,
            sensitive=sensitive,
        )

    if kind == "say":
        return None
    if kind == "slide":
        return None
    if kind == "desktop":
        return None
    if kind == "closeWindow":
        # Closing the active page is the whole action; the caller's post-step
        # lifecycle check reverts `active_page` to the main window.
        await page.close()
        return None
    if kind == "navigate":
        url = step.navigate_url()
        assert url is not None  # guaranteed by command_kind()
        await recorder.navigate(_resolve_url(scenario, url))
        return None
    if kind == "wait" and not step.requires_target():
        await recorder.wait_seconds(float(step.wait))
        return None
    if kind == "scroll":
        await recorder.scroll(step.scroll_config())
        return None

    # step that needs a target
    if isinstance(cached_in, PendingAction):
        if _can_reuse(cached_in, step, chash, force):
            # An optional element that was absent last time stays pending: retrying
            # would burn the full gate timeout on every compile. `--force` retries.
            if verbose:
                tqdm.write("   ↳ pending (nadal opcjonalny, nierozwiązany)")
            return cached_in
        cached_in = None

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
        try:
            resolved = await resolve_step_target(page, step, kind, reasoner)
        except TargetResolutionError as exc:
            # Every resolver verdict lands here, and every one of them names
            # something the author must edit in the scenario: an option the
            # `<select>` does not offer, a dropdown the page hides with nothing
            # visible in its place, an ambiguous description. The resolver has
            # no business knowing about source maps, so the banner is applied at
            # the dispatch site — and applied to *all* verdicts, so a `select:`
            # step and a `click:` step in the same file are diagnosed alike.
            #
            # Deliberately the named verdict type, not ``RuntimeError``: an
            # injected reasoner raises through this same frame (``RaisingReasoner``
            # signals ``SetupNeedsCompile``, itself a ``RuntimeError``), and
            # rewrapping that would turn control flow into a step diagnosis.
            raise RuntimeError(step_message(str(exc))) from exc
        if isinstance(resolved, TargetAbsent):
            if not optional:
                raise RuntimeError(step_message(resolved.error_message))
            return _pending_for(step, chash)
        assert isinstance(resolved, ResolvedTarget)
        action, target, input_text = resolved.action, resolved.target, resolved.input_text
        state, identity = resolved.state, resolved.identity
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
            # The click *itself* tolerates the window it closed — see
            # ``Recorder.click``, which both compile and render go through. What
            # is left for this layer is the run-up: resolving and pointing at a
            # target on a page that a previous step's drift already tore down.
            # That is still this window's death rather than a distinct failure,
            # so hand it to the caller's lifecycle checks the same way. Any
            # failure with the page still open raises.
            if not page.is_closed():
                raise
    elif action == "hover":
        await recorder.hover(target)
    elif action == "type":
        text = step.enter_text.text if step.enter_text is not None else input_text
        if text is None:
            raise RuntimeError("brak tekstu dla akcji type")
        await recorder.enter_text(target, text)
    elif action == "select":
        if step.select is None:
            raise RuntimeError(step_message("brak opcji dla akcji select"))
        try:
            await recorder.select(
                target,
                step.select.option,
                native=select_mode(step, scenario.config) == "native",
            )
        except (SelectDriveError, SelectsNotReadyError) as exc:
            # Compile probes drivability so an undriveable widget surfaces here,
            # before a multi-minute render is paid for. Both failures point at
            # the same YAML: the step whose dropdown could not be driven, or the
            # `config.selects` block whose widget never settled — so both arrive
            # through the banner, with `plik:linia` and the fragment.
            raise RuntimeError(step_message(str(exc))) from exc
    elif action == "highlight":
        # Nothing to perform: the command only marks the target, which compile has
        # already resolved and frozen. Spelled out rather than left to fall off the
        # end of the chain, so the no-op reads as a decision, not an omission.
        pass
    elif action == "waitFor":
        timeout = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
        try:
            await recorder.wait_for(target, state or "visible", timeout)
        except PlaywrightTimeoutError:
            # The other half of the error boundary: an elapsed wait window on an
            # optional step means "absent", anything else still fails the compile.
            if not optional:
                raise
            return _pending_for(step, chash)
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
                compiled_from=compiled_from(step),
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
