"""One compile step: resolve a target if needed, perform the action, freeze it.

This module owns :func:`_compile_step` and nothing else, so that the one place
that decides *what a step does* stays readable next to the one place that decides
*whether the frozen answer may be reused* (:mod:`~guidebot_recorder.recorder.compile.cache`).

The step runs as four phases over one :class:`_StepContext`: the targetless
dispatch (:func:`_perform_targetless`), the cache-or-resolve decision
(:func:`_settle_target`), the action itself (:func:`_perform_action`, over the
:data:`_ACTIONS` table), and the freeze plus readiness barrier
(:func:`_freeze`, :func:`_await_readiness`). The context carries everything the
phases share, so none of them needs a parameter list that restates the step.

**Test seam.** ``resolve_step_target`` is imported by name into this module's
globals on purpose: :func:`_resolve_fresh` reads it from *here* at call time, so
this module is the one a test must patch::

    monkeypatch.setattr(compile_module.step, "resolve_step_target", fake)

The package facade withholds the name for that reason — see
:mod:`guidebot_recorder.recorder.compile`. Do not re-import it anywhere else in
the package: a second binding is a second copy, and one patch would then cover
only one of them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

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
    ActionKind,
    CachedAction,
    Expect,
    Fingerprint,
    PendingAction,
    WaitState,
)
from guidebot_recorder.models.compiled import CompiledAction
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WaitUntil, select_mode
from guidebot_recorder.models.target import Target
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

#: Command kinds a compile step records nothing for and performs nothing on.
#: Named rather than left to fall off the end of :func:`_perform_targetless`, so
#: that "this kind is deliberately inert" and "this kind is not handled here"
#: stay two different statements.
_INERT_KINDS = frozenset({"say", "slide", "desktop"})


@dataclass
class _Resolution:
    """What this step will do — from the sidecar, or from a fresh resolve.

    One object for the cluster of values the old body rebound in two places
    (once on the reuse path, once on the resolve path) and then read from four:
    the action ladder, the freeze, the readiness barrier, and the return.

    ``expect`` and ``cached`` arrive already filled on the reuse path and are
    settled by :func:`_freeze` on the fresh one — which is why they are the two
    fields with defaults, and why ``fresh`` has to be carried rather than
    inferred from ``cached is None``.
    """

    action: ActionKind
    target: Target
    state: WaitState | None
    input_text: str | None
    identity: Identity | None
    expect: Expect | None = None
    fresh: bool = False
    cached: CachedAction | None = None


@dataclass
class _StepContext:
    """Everything one compile step carries from dispatch through to freeze.

    The fields above ``plan`` are the step's fixed inputs — :func:`_compile_step`
    fills them once and nothing rebinds them. The three below it are the state
    the phases hand to each other: the settled :class:`_Resolution`, and the URLs
    bracketing the action that :func:`heuristic_expect` reads to decide whether
    the step navigated.
    """

    page: Page
    recorder: Recorder
    scenario: Scenario
    chash: str
    index: int
    step: Step
    kind: str
    reasoner: Reasoner
    before_click: Callable[[], None]
    force: bool
    verbose: bool
    optional: bool
    entry: FlatStep | None
    total: int
    sensitive: Iterable[str]

    plan: _Resolution | None = None
    url_before: str = ""
    url_after: str = ""

    def message(self, message: str) -> str:
        """Komunikat kroku z `plik:linia` i fragmentem YAML; sekrety zredagowane."""

        return step_banner(
            index=self.index,
            total=self.total,
            location=self.entry.location if self.entry is not None else None,
            source=self.scenario.source,
            message=message,
            sensitive=self.sensitive,
        )


async def _perform_targetless(ctx: _StepContext) -> bool:
    """Run a step that needs no target, reporting whether it was one.

    Returning a bool rather than the sidecar entry is what keeps this honest:
    every kind here freezes ``None``, and the caller must still tell "handled,
    nothing to freeze" from "not mine, carry on to the target phase".
    """

    kind = ctx.kind
    if kind in _INERT_KINDS:
        return True
    if kind == "closeWindow":
        # Closing the active page is the whole action; the caller's post-step
        # lifecycle check reverts `active_page` to the main window.
        await ctx.page.close()
        return True
    if kind == "navigate":
        url = ctx.step.navigate_url()
        assert url is not None  # guaranteed by command_kind()
        await ctx.recorder.navigate(_resolve_url(ctx.scenario, url))
        return True
    if kind == "wait" and not ctx.step.requires_target():
        await ctx.recorder.wait_seconds(float(ctx.step.wait))
        return True
    if kind == "scroll":
        await ctx.recorder.scroll(ctx.step.scroll_config())
        return True
    return False


async def _settle_target(
    ctx: _StepContext, cached_in: CompiledAction | None
) -> _Resolution | PendingAction:
    """Decide between reusing the frozen answer and asking the resolver again.

    A :class:`PendingAction` comes back when the step has no target to act on —
    either the sidecar's pending entry is still good, or the resolver reported
    the optional element absent again. Either way the caller freezes it and
    performs nothing.
    """

    if isinstance(cached_in, PendingAction):
        if _can_reuse(cached_in, ctx.step, ctx.chash, ctx.force):
            # An optional element that was absent last time stays pending: retrying
            # would burn the full gate timeout on every compile. `--force` retries.
            if ctx.verbose:
                tqdm.write("   ↳ pending (nadal opcjonalny, nierozwiązany)")
            return cached_in
        cached_in = None

    # ``isinstance`` is not belt-and-braces: the pending branch above either
    # returned or cleared ``cached_in``, but both ``reuse_is_valid`` and
    # ``pinned_drifted`` read ``.target``, which a :class:`PendingAction` does
    # not have. Stating the type here keeps that invariant local and checked.
    if (
        _can_reuse(cached_in, ctx.step, ctx.chash, ctx.force)
        and isinstance(cached_in, CachedAction)
        and await reuse_is_valid(ctx.page, cached_in)
        # A frozen index is only worth as much as the page it was measured
        # against. Drift means it now points somewhere else, so the entry is
        # dropped and the fresh resolution below measures the index anew.
        and not await pinned_drifted(ctx.page, cached_in)
    ):
        if ctx.verbose:
            tqdm.write("   ↳ reuse (cache)")
        return _Resolution(
            action=cached_in.action,
            target=cached_in.target,
            state=cached_in.state,
            input_text=cached_in.input_text,
            identity=cached_in.identity,
            expect=cached_in.expect,
            cached=cached_in,
        )
    return await _resolve_fresh(ctx)


async def _resolve_fresh(ctx: _StepContext) -> _Resolution | PendingAction:
    """Ask the resolver for this step's target, or record it as absent."""

    try:
        resolved = await resolve_step_target(ctx.page, ctx.step, ctx.kind, ctx.reasoner)
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
        raise RuntimeError(ctx.message(str(exc))) from exc
    if isinstance(resolved, TargetAbsent):
        if not ctx.optional:
            raise RuntimeError(ctx.message(resolved.error_message))
        return _pending_for(ctx.step, ctx.chash)
    assert isinstance(resolved, ResolvedTarget)
    if ctx.verbose:
        tqdm.write(f"   ↳ {resolved.action} → {_target_desc(resolved.target)}")
    if resolved.pinned is not None and resolved.pinned.index is not None:
        _warn_positional(
            ctx.index,
            resolved.pinned,
            total=ctx.total,
            location=ctx.entry.location if ctx.entry is not None else None,
            source=ctx.scenario.source,
            sensitive=ctx.sensitive,
        )
    return _Resolution(
        action=resolved.action,
        target=resolved.target,
        state=resolved.state,
        input_text=resolved.input_text,
        identity=resolved.identity,
        fresh=True,
    )


async def _do_click(ctx: _StepContext) -> CompiledAction | None:
    try:
        await ctx.recorder.click(ctx.plan.target, before_click=ctx.before_click)
    except PlaywrightError:
        # The click *itself* tolerates the window it closed — see
        # ``Recorder.click``, which both compile and render go through. What
        # is left for this layer is the run-up: resolving and pointing at a
        # target on a page that a previous step's drift already tore down.
        # That is still this window's death rather than a distinct failure,
        # so hand it to the caller's lifecycle checks the same way. Any
        # failure with the page still open raises.
        if not ctx.page.is_closed():
            raise
    return None


async def _do_hover(ctx: _StepContext) -> CompiledAction | None:
    await ctx.recorder.hover(ctx.plan.target)
    return None


async def _do_type(ctx: _StepContext) -> CompiledAction | None:
    text = ctx.step.enter_text.text if ctx.step.enter_text is not None else ctx.plan.input_text
    if text is None:
        raise RuntimeError("brak tekstu dla akcji type")
    await ctx.recorder.enter_text(ctx.plan.target, text)
    return None


async def _do_select(ctx: _StepContext) -> CompiledAction | None:
    if ctx.step.select is None:
        raise RuntimeError(ctx.message("brak opcji dla akcji select"))
    try:
        await ctx.recorder.select(
            ctx.plan.target,
            ctx.step.select.option,
            native=select_mode(ctx.step, ctx.scenario.config) == "native",
        )
    except (SelectDriveError, SelectsNotReadyError) as exc:
        # Compile probes drivability so an undriveable widget surfaces here,
        # before a multi-minute render is paid for. Both failures point at
        # the same YAML: the step whose dropdown could not be driven, or the
        # `config.selects` block whose widget never settled — so both arrive
        # through the banner, with `plik:linia` and the fragment.
        raise RuntimeError(ctx.message(str(exc))) from exc
    return None


async def _do_highlight(ctx: _StepContext) -> CompiledAction | None:
    # Nothing to perform: the command only marks the target, which compile has
    # already resolved and frozen. Spelled out rather than left out of the table,
    # so the no-op reads as a decision, not an omission.
    return None


async def _do_wait_for(ctx: _StepContext) -> CompiledAction | None:
    timeout = ctx.step.wait.timeout if isinstance(ctx.step.wait, WaitUntil) else 10.0
    try:
        await ctx.recorder.wait_for(ctx.plan.target, ctx.plan.state or "visible", timeout)
    except PlaywrightTimeoutError:
        # The other half of the error boundary: an elapsed wait window on an
        # optional step means "absent", anything else still fails the compile.
        if not ctx.optional:
            raise
        return _pending_for(ctx.step, ctx.chash)
    return None


#: One handler per resolvable action, taking the whole step context so that no
#: handler needs a parameter list restating the step. Each returns the sidecar
#: entry that ends the step early, or ``None`` to carry on to the freeze — only
#: ``waitFor`` ever uses the first half, but a uniform protocol is what lets the
#: dispatch stay a table lookup instead of another ladder.
_ACTIONS: dict[str, Callable[[_StepContext], Awaitable[CompiledAction | None]]] = {
    "click": _do_click,
    "hover": _do_hover,
    "type": _do_type,
    "select": _do_select,
    "highlight": _do_highlight,
    "waitFor": _do_wait_for,
}


async def _perform_action(ctx: _StepContext) -> CompiledAction | None:
    """Perform the resolved action, which reveals the state later steps expect.

    An action with no handler performs nothing and is not an error — the same
    silence the ``if``/``elif`` ladder this table replaced had, for want of an
    ``else``. Changing that is a behavioural fix with its own test, not a side
    effect of the dispatch shape.
    """

    ctx.url_before = ctx.page.url
    handler = _ACTIONS.get(ctx.plan.action)
    early = await handler(ctx) if handler is not None else None
    # Only when the step carries on: an early sidecar entry is frozen as it
    # stands, so reading the page again would be a question nobody asks.
    if early is None:
        ctx.url_after = ctx.page.url if not ctx.page.is_closed() else ctx.url_before
    return early


def _freeze(ctx: _StepContext) -> None:
    """Build the sidecar entry for a fresh resolve; a reused one already has one.

    ``expect`` is only knowable here: it is read off the two URLs the action ran
    between, which is why it is neither resolved nor performed earlier.
    """

    plan = ctx.plan
    if not plan.fresh:
        return
    plan.expect = heuristic_expect(ctx.url_before, ctx.url_after)
    plan.cached = CachedAction(
        action=plan.action,
        target=plan.target,
        identity=plan.identity,
        expect=plan.expect,
        state=plan.state,
        input_text=plan.input_text,
        fingerprint=Fingerprint(
            command_kind=ctx.kind,
            compiled_from=compiled_from(ctx.step),
            expect=plan.expect,
            config_hash=ctx.chash,
            state=plan.state,
        ),
    )


async def _await_readiness(ctx: _StepContext) -> None:
    """Let the page settle the way the frozen ``expect`` says it should."""

    if ctx.page.is_closed():
        return
    try:
        await ctx.recorder.apply_readiness(ctx.plan.expect)
    except PlaywrightError:
        if not ctx.page.is_closed():
            raise


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

    ctx = _StepContext(
        page=page,
        recorder=recorder,
        scenario=scenario,
        chash=chash,
        index=index,
        step=step,
        kind=kind,
        reasoner=reasoner,
        before_click=before_click,
        force=force,
        verbose=verbose,
        optional=optional,
        entry=entry,
        total=total,
        sensitive=sensitive,
    )
    if await _perform_targetless(ctx):
        return None
    plan = await _settle_target(ctx, cached_in)
    if isinstance(plan, PendingAction):
        return plan
    ctx.plan = plan
    early = await _perform_action(ctx)
    if early is not None:
        return early
    _freeze(ctx)
    await _await_readiness(ctx)
    return plan.cached
