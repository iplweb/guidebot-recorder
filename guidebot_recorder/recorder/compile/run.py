"""The compile loop itself: the session, the step sequence, the checkpoints.

:func:`run_compile` drives an already-prepared page; :func:`run_compile_in_browser`
is the thin wrapper that builds the context first, at the locale and viewport the
render phase will later replay. Both stay here because they share one invariant —
the compile context must be indistinguishable from the render context, or frozen
targets do not line up.

The loop itself is two objects and a handful of phases. :class:`CompileSession`
holds what outlives a step — the sidecar being built, whether it still owes a
write, which branch is being skipped, the page watch, the progress bar — and
:class:`_StepRun` holds what one turn of the loop needs, including the click
observation window that decides whether a page belongs to the click that just
ran. Every phase below takes one of those two and nothing else, which is what
keeps their signatures from restating the session.

Everything the loop delegates lives one module away: reuse decisions in
:mod:`~guidebot_recorder.recorder.compile.cache`, the popup session contract in
:mod:`~guidebot_recorder.recorder.compile.pages`, the per-step work in
:mod:`~guidebot_recorder.recorder.compile.step`, and the human-facing text in
:mod:`~guidebot_recorder.recorder.compile.describe`.

**Test seam.** ``write_compiled`` is imported by name into this module's globals
on purpose: :meth:`CompileSession.checkpoint` reads it from *here* at call time
(a method defined in this module resolves its globals in this module), so this
module is the one a test must patch::

    monkeypatch.setattr(compile_module.run, "write_compiled", counting_write)

The package facade withholds the name for that reason — see
:mod:`guidebot_recorder.recorder.compile`. Do not re-import it anywhere else in
the package: a second binding is a second copy, and one patch would then cover
only one of them. It is also why the session lives here rather than in a state
module of its own: moving ``checkpoint`` would move the seam with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from playwright.async_api import Browser, Page
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import (
    CachedAction,
    PendingAction,
    validate_teach_instruction,
)
from guidebot_recorder.models.compiled import CompiledAction, CompiledScenario
from guidebot_recorder.models.config import config_hash, site_viewport
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step
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
    _PageWatch,
    _prepare_popup,
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


@dataclass
class CompileSession:
    """What outlives a single step of the compile loop.

    The mutable half is the point: :attr:`actions` is the sidecar being built,
    :attr:`artifact_dirty` remembers whether it still owes a write, and
    :attr:`skipped_branch` is the gate whose children are being frozen unresolved.
    Everything else is settled once, before the loop starts, and only read.
    """

    path: Path
    scenario: Scenario
    chash: str
    flat: list[FlatStep]
    cpath: Path
    actions: list[CompiledAction | None]
    pages: _PageWatch
    bar: tqdm
    sensitive: tuple[str, ...]
    selects: Selects | None
    reasoner: Reasoner
    timeout: float
    force: bool
    verbose: bool
    pause_on_error: bool
    artifact_dirty: bool = False
    #: The branch whose gate turned out to be absent — its children are recorded
    #: pending rather than executed.
    skipped_branch: int | None = None

    def checkpoint(self) -> None:
        """Persist only semantic progress, while keeping fresh resolves crash-safe."""

        write_compiled(self.cpath, CompiledScenario(source=self.path.name, actions=self.actions))
        self.artifact_dirty = False

    def banner(self, entry: FlatStep, index: int, message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=index,
            total=len(self.flat),
            location=entry.location,
            source=self.scenario.source,
            message=message,
            sensitive=self.sensitive,
        )


@dataclass
class _StepRun:
    """One turn of the compile loop, and the click observation window it opens.

    The window is why this is an object rather than four locals: ``before_click``
    fires from inside :func:`_compile_step`, and the marks it takes there
    (:attr:`click_observed_start`, :attr:`click_started_at`) are read back here
    afterwards to decide which pages the click is answerable for.
    """

    session: CompileSession
    index: int
    entry: FlatStep
    recorder: Recorder
    kind: str
    #: The context's pages as they stood before the action, and the length of the
    #: observation log at the same instant. Taken together they survive a page
    #: that opens and closes inside the step.
    pages_before: tuple[Page, ...] = ()
    observed_start: int = 0
    click_observed_start: int | None = None
    click_started_at: float | None = None
    #: The window the action ran on, and whether it was gone by the time the
    #: action returned — the pair that tells an action-driven close from an
    #: asynchronous one.
    action_page: Page | None = None
    action_page_closed_in_window: bool = False

    @property
    def step(self) -> Step:
        return self.entry.step

    def banner(self, message: str) -> str:
        """This step's message, with `plik:linia` and the YAML fragment."""

        return self.session.banner(self.entry, self.index, message)

    def open_observation_window(self) -> None:
        """Mark what the session held just before the action is performed."""

        watch = self.session.pages
        self.pages_before = tuple(watch.main.context.pages)
        self.observed_start = len(watch.observed)
        self.action_page = watch.active

    def arm_click(self) -> None:
        """Assign only pages created after this point to the click."""

        watch = self.session.pages
        if watch.since(self.observed_start):
            raise RuntimeError(
                "nieoczekiwany popup otworzył się podczas rozwiązywania kroku, przed akcją click"
            )
        self.click_observed_start = len(watch.observed)
        self.click_started_at = watch.loop.time()

    @property
    def click_armed(self) -> bool:
        return self.click_observed_start is not None and self.click_started_at is not None


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


def _record_skipped_child(session: CompileSession, index: int, entry: FlatStep) -> bool:
    """Freeze a child of an absent gate as pending, reporting whether it was one.

    The gate never showed up, so the child is recorded pending — a later render
    can resolve it in place — and is not executed now. Clearing ``skipped_branch``
    on the way out is what ends the skip at the first step outside the branch.
    """

    if session.skipped_branch is None or entry.branch != session.skipped_branch:
        session.skipped_branch = None
        return False
    step = entry.step
    action_before = session.actions[index]
    session.actions[index] = _pending_for(step, session.chash) if step.requires_target() else None
    if session.actions[index] != action_before:
        session.artifact_dirty = True
        session.checkpoint()
    session.bar.update(1)
    return True


def _assert_session_intact(session: CompileSession, index: int, entry: FlatStep) -> None:
    """Both windows are alive and nothing opened outside the session contract."""

    watch = session.pages
    if watch.main.is_closed():
        raise RuntimeError("główne okno zostało zamknięte podczas compile")
    if watch.active.is_closed():
        raise RuntimeError("popup zamknął się poza obsługiwaną akcją scenariusza")
    if watch.unexpected():
        raise RuntimeError(session.banner(entry, index, "nieoczekiwany popup poza akcją click"))


def _reject_impossible_command(run: _StepRun) -> None:
    """Refuse a command this session cannot honour, before anything is performed."""

    if run.kind == "closeWindow" and run.session.pages.active is run.session.pages.main:
        raise RuntimeError(run.banner("closeWindow bez otwartego okna"))
    if run.kind == "teach":
        try:
            validate_teach_instruction(_instruction(run.step))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc


async def _await_selects_ready(run: _StepRun) -> None:
    """Readiness barrier: resolve against the shimmed DOM, or not at all.

    The resolver's page snapshot must be taken against the shimmed DOM (the
    navigation that led here has settled by now — it was an earlier step).
    Without it compile can freeze a target the render, running with the widget in
    place, no longer recognises.
    """

    session = run.session
    if session.selects is None or not run.step.requires_target():
        return
    try:
        await session.selects.wait_ready(session.pages.active)
    except SelectsNotReadyError as exc:
        # A wedged widget is a step-level failure like any other, so it arrives
        # with `plik:linia` and the YAML fragment rather than beside them. The
        # two fixes the message names (`selects.settleMs`, `selects.mode:
        # native`) are both edits to the scenario file.
        raise RuntimeError(run.banner(str(exc))) from exc


async def _pages_opened_by(run: _StepRun, compiled_action: CompiledAction | None) -> list[Page]:
    """The pages this step's click opened — none, for any other action."""

    if isinstance(compiled_action, CachedAction) and compiled_action.action == "click":
        if not run.click_armed:  # pragma: no cover - dispatch invariant
            raise RuntimeError("wewnętrzny błąd obserwacji akcji click")
        return await _wait_for_new_pages(
            run.session.pages.main.context,
            run.pages_before,
            run.session.pages.observed,
            run.click_observed_start,
            run.session.pages.opened_at,
            started_at=run.click_started_at,
        )
    if run.session.pages.since(run.observed_start):
        raise RuntimeError("popup może zostać otwarty tylko przez akcję click")
    return []


async def _adopt_popup(
    run: _StepRun, compiled_action: CompiledAction | None
) -> CompiledAction | None:
    """Take over the window the click opened, or clear a stale popup flag."""

    new_pages = await _pages_opened_by(run, compiled_action)
    if len(new_pages) > 1:
        raise RuntimeError("v1 obsługuje dokładnie jeden popup w sesji")
    watch = run.session.pages
    if not new_pages:
        if isinstance(compiled_action, CachedAction) and compiled_action.opens_popup:
            # Refresh observed lifecycle metadata even when the target/action
            # itself was safely reused.
            return compiled_action.model_copy(update={"opens_popup": False})
        return compiled_action
    if watch.popup_seen:
        raise RuntimeError("v1 obsługuje co najwyżej jeden popup w całej sesji")
    openers = [await candidate.opener() for candidate in new_pages]
    if any(opener is not watch.active for opener in openers):
        raise RuntimeError("nowa strona nie jest popupem aktywnego okna")
    watch.popup_seen = True
    popup = new_pages[0]
    watch.popup = popup
    adopted = compiled_action.model_copy(update={"opens_popup": True})
    popup.set_default_timeout(run.session.timeout * 1000)
    viewport = run.session.scenario.config.viewport
    prepared = await _prepare_popup(popup, {"width": viewport.width, "height": viewport.height})
    if not prepared:
        raise RuntimeError("popup zamknął się podczas otwierania")
    watch.active = popup
    return adopted


async def _settle_windows(run: _StepRun, compiled_action: CompiledAction | None) -> None:
    """Re-establish the session contract after the action ran."""

    watch = run.session.pages
    if watch.main.is_closed():
        raise RuntimeError("główne okno zostało zamknięte podczas compile")
    if watch.active.is_closed():
        close_was_action_driven = run.kind == "closeWindow" or (
            watch.active is run.action_page
            and run.action_page_closed_in_window
            and isinstance(compiled_action, CachedAction)
            and compiled_action.action in {"click", "hover", "type"}
        )
        if not close_was_action_driven:
            raise RuntimeError("popup zamknął się asynchronicznie poza obsługiwaną akcją")
        watch.active = watch.main
        await watch.active.bring_to_front()
        await Recorder(watch.active, overlay=None).apply_readiness("none")
        await watch.active.wait_for_load_state()
    if watch.unexpected():
        raise RuntimeError(run.banner("nieoczekiwany dodatkowy popup"))


async def _resolve_and_perform(run: _StepRun) -> None:
    """Compile one step, adopt whatever it opened, and record the result."""

    session = run.session
    await _await_selects_ready(run)
    run.open_observation_window()
    compiled_action = await _compile_step(
        session.pages.active,
        run.recorder,
        session.scenario,
        session.chash,
        run.index,
        run.step,
        run.kind,
        session.reasoner,
        session.actions[run.index],
        before_click=run.arm_click,
        force=session.force,
        verbose=session.verbose,
        optional=run.entry.is_gate or run.step.optional,
        entry=run.entry,
        total=len(session.flat),
        sensitive=session.sensitive,
    )
    run.action_page_closed_in_window = run.action_page.is_closed()
    compiled_action = await _adopt_popup(run, compiled_action)
    await _settle_windows(run, compiled_action)
    session.actions[run.index] = compiled_action
    if isinstance(compiled_action, PendingAction):
        _warn_absent(
            run.index,
            run.step,
            gate=run.entry.is_gate,
            total=len(session.flat),
            location=run.entry.location,
            source=session.scenario.source,
            sensitive=session.sensitive,
        )
        if run.entry.is_gate:
            session.skipped_branch = run.entry.branch


async def _fail_step(run: _StepRun, exc: Exception) -> NoReturn:
    """Redact, optionally pause for inspection, and re-raise without the traceback."""

    session = run.session
    safe_message = redact_exception(exc, session.sensitive)
    if session.verbose:
        tqdm.write(f"   ✗ {type(exc).__name__}: {safe_message}")
    if session.pause_on_error:
        watch = session.pages
        debug_page = watch.active if not watch.active.is_closed() else watch.main
        await pause_for_inspection(
            debug_page,
            "compile",
            run.index,
            run.kind,
            exc,
            session.sensitive,
            total=len(session.flat),
            location=run.entry.location,
            source=session.scenario.source,
        )
    raise RuntimeError(f"{type(exc).__name__}: {safe_message}") from None


async def _compile_one_step(session: CompileSession, index: int, entry: FlatStep) -> None:
    """One turn of the loop: guard the session, compile the step, checkpoint it."""

    _assert_session_intact(session, index, entry)
    session.pages.active.set_default_timeout(session.timeout * 1000)
    await session.pages.active.bring_to_front()
    run = _StepRun(
        session=session,
        index=index,
        entry=entry,
        recorder=Recorder(session.pages.active, overlay=None),
        kind=entry.step.command_kind(),
    )
    _reject_impossible_command(run)
    if session.verbose:
        description = redact_text(_short(run.step), session.sensitive)
        tqdm.write(f"[{index + 1}/{len(session.flat)}] {run.kind}: {description}")
    action_before = session.actions[index]
    try:
        await _resolve_and_perform(run)
    except Exception as exc:
        await _fail_step(run, exc)
    if session.actions[index] != action_before:
        # A fresh resolve (or refreshed popup lifecycle metadata) is real
        # progress and remains checkpointed before the next step. Pure
        # cache hits and targetless steps do not rewrite the full sidecar.
        session.artifact_dirty = True
        session.checkpoint()
    session.bar.update(1)


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
    # CRUCIAL: the same viewport as render, otherwise frozen positions do not match.
    # With chrome enabled that is the shell iframe interior (``H - chrome.height``).
    site_width, site_height = site_viewport(cfg)
    await page.set_viewport_size({"width": site_width, "height": site_height})
    page.set_default_timeout(timeout * 1000)
    context = page.context
    watch = _PageWatch.starting_at(page)
    # Held in a local rather than re-read as ``watch.observe``: each attribute
    # access builds a fresh bound method, and the listener registry has to be
    # handed back the very object it was given.
    listener = watch.observe
    context.on("page", listener)

    # Flat indexing: a `when:` block contributes its synthetic gate step followed by
    # its children, so `actions` stays positionally 1:1 with what actually executes.
    flat = scenario.flat_steps()
    cpath = compiled_path(path)
    session = CompileSession(
        path=path,
        scenario=scenario,
        chash=config_hash(cfg),
        flat=flat,
        cpath=cpath,
        actions=_load_prior_actions(cpath, len(flat)),
        artifact_dirty=not _compiled_artifact_is_current(cpath, path.name, len(flat)),
        pages=watch,
        bar=tqdm(total=len(flat), desc="compile", unit="krok", disable=not verbose),
        sensitive=sensitive_values,
        selects=selects,
        reasoner=reasoner,
        timeout=timeout,
        force=force,
        verbose=verbose,
        pause_on_error=pause_on_error,
    )
    try:
        for index, entry in enumerate(flat):
            if _record_skipped_child(session, index, entry):
                continue
            await _compile_one_step(session, index, entry)
        if flat and session.artifact_dirty:
            # Targetless scenarios and artifact-only repairs still need one final,
            # aligned sidecar even though no per-step action changed.
            session.checkpoint()
    finally:
        context.remove_listener("page", listener)
        session.bar.close()
