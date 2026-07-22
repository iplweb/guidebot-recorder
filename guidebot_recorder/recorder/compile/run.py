"""The compile loop itself: the session, the step sequence, the checkpoints.

:func:`run_compile` drives an already-prepared page; :func:`run_compile_in_browser`
is the thin wrapper that builds the context first, at the locale and viewport the
render phase will later replay. Both stay here because they share one invariant —
the compile context must be indistinguishable from the render context, or frozen
targets do not line up.

Everything the loop delegates lives one module away: reuse decisions in
:mod:`~guidebot_recorder.recorder.compile.cache`, the popup session contract in
:mod:`~guidebot_recorder.recorder.compile.pages`, the per-step work in
:mod:`~guidebot_recorder.recorder.compile.step`, and the human-facing text in
:mod:`~guidebot_recorder.recorder.compile.describe`. Its complexity is
deliberately untouched by the package split.

**Test seam.** ``write_compiled`` is imported by name into this module's globals
on purpose: the ``checkpoint`` closure inside :func:`run_compile` reads it from
*here* at call time, so this module is the one a test must patch::

    monkeypatch.setattr(compile_module.run, "write_compiled", counting_write)

The package facade withholds the name for that reason — see
:mod:`guidebot_recorder.recorder.compile`. Do not re-import it anywhere else in
the package: a second binding is a second copy, and one patch would then cover
only one of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from playwright.async_api import Browser, Page
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import (
    CachedAction,
    PendingAction,
    validate_teach_instruction,
)
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import config_hash, site_viewport
from guidebot_recorder.models.scenario import FlatStep
from guidebot_recorder.recorder._debug import (
    pause_for_inspection,
    redact_exception,
    redact_text,
    scenario_sensitive_values,
)
from guidebot_recorder.recorder.compile.cache import (
    _compiled_artifact_is_current,
    _load_prior_actions,
    _pending_for,
)
from guidebot_recorder.recorder.compile.describe import _short, _warn_absent
from guidebot_recorder.recorder.compile.pages import (
    _prepare_popup,
    _unexpected_pages,
    _wait_for_new_pages,
)
from guidebot_recorder.recorder.compile.step import _compile_step
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import (
    step_instruction as _instruction,
)
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.selects import Selects, SelectsNotReadyError, install_selects


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
    # is imported lazily: ``session`` imports ``run_compile`` from this package's
    # facade at its top, so a module-level import here would form a cycle.
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
