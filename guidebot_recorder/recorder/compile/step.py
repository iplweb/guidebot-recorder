"""One compile step: resolve a target if needed, perform the action, freeze it.

This module owns :func:`_compile_step` and nothing else, so that the one place
that decides *what a step does* stays readable next to the one place that decides
*whether the frozen answer may be reused* (:mod:`~guidebot_recorder.recorder.compile.cache`).
Its complexity is deliberately untouched by the package split; decomposing it is
a separate piece of work.

**Test seam.** ``resolve_step_target`` is imported by name into this module's
globals on purpose: :func:`_compile_step` reads it from *here* at call time, so
this module is the one a test must patch::

    monkeypatch.setattr(compile_module.step, "resolve_step_target", fake)

The package facade withholds the name for that reason — see
:mod:`guidebot_recorder.recorder.compile`. Do not re-import it anywhere else in
the package: a second binding is a second copy, and one patch would then cover
only one of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import Page
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.models.action import (
    CachedAction,
    Fingerprint,
    PendingAction,
)
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
from guidebot_recorder.recorder.compile.cache import _can_reuse, _pending_for
from guidebot_recorder.recorder.compile.describe import (
    _resolve_url,
    _target_desc,
    _warn_positional,
)
from guidebot_recorder.recorder.recorder import Recorder, SelectDriveError
from guidebot_recorder.resolver.positional import pinned_drifted
from guidebot_recorder.resolver.reasoner import Reasoner
from guidebot_recorder.resolver.resolution import (
    ResolvedTarget,
    TargetAbsent,
    TargetResolutionError,
    compiled_from,
    heuristic_expect,
    resolve_step_target,
)
from guidebot_recorder.resolver.validate import reuse_is_valid
from guidebot_recorder.selects import SelectsNotReadyError


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

    # ``isinstance`` is not belt-and-braces: the pending branch above either
    # returned or cleared ``cached_in``, but both ``reuse_is_valid`` and
    # ``pinned_drifted`` read ``.target``, which a :class:`PendingAction` does
    # not have. Stating the type here keeps that invariant local and checked.
    if (
        _can_reuse(cached_in, step, chash, force)
        and isinstance(cached_in, CachedAction)
        and await reuse_is_valid(page, cached_in)
        # A frozen index is only worth as much as the page it was measured
        # against. Drift means it now points somewhere else, so the entry is
        # dropped and the fresh resolution below measures the index anew.
        and not await pinned_drifted(page, cached_in)
    ):
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
        if resolved.pinned is not None and resolved.pinned.index is not None:
            _warn_positional(
                index,
                resolved.pinned,
                total=total,
                location=entry.location if entry is not None else None,
                source=scenario.source,
                sensitive=sensitive,
            )

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
