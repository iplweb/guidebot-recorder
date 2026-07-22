"""Live capture pass: replay the compiled scenario and screenshot each step.

:func:`capture_pages` is the loop. One turn of it builds a
:class:`~guidebot_recorder.guide.replay._StepRun` and hands it to the phase for
that page kind (:data:`_PAGES`); an action step then goes on through the frozen
action's own phase (``replay._FRAMES``). The state those phases share —
including the cursor trail whose ordering invariant is the point of the whole
arrangement — lives in :mod:`~guidebot_recorder.guide.replay` and
:mod:`~guidebot_recorder.guide.trail`.

**Test seams, and why the split stops where it does.** ``reuse_failure``,
``annotations_for`` and ``pause_for_inspection`` are patched on *this* module::

    monkeypatch.setattr(capture, "reuse_failure", fake)

so the three phases that consume them — :func:`_reject_unusable_target`,
:func:`_append_step_page` and :func:`capture_pages` itself — have to stay here.
Moved to a sibling module they would read *that* module's globals, and every one
of those patches would succeed and reach nobody: green tests asserting nothing.
``tests/unit/guide/test_capture_seams.py`` asserts this file still calls each
name a test patches on it, so the mistake fails loudly instead of quietly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.guide.annotate import annotations_for, cursor_shape
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import GuideError, classify
from guidebot_recorder.guide.replay import (
    _FRAMES,
    _approach_target,
    _Capture,
    _click_or_hover_frame,
    _gate_page,
    _navigate_page,
    _scroll_page,
    _slide_page,
    _StepRun,
    _text_page,
    _wait_page,
    scenario_resolve_url,
)
from guidebot_recorder.models.action import CachedAction

# Imported as a bare name (not `from ... import _debug`) so it becomes an attribute
# of this module and tests can monkeypatch it, like `reuse_failure` below.
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_failure
from guidebot_recorder.selects import Selects

#: ``scenario_resolve_url`` moved to :mod:`~guidebot_recorder.guide.replay` with
#: its only caller but stays importable from here, where it was public before the
#: split. It is not a seam — nothing patches it — so re-exporting is safe; the
#: rule that forbids re-exporting a *patched* name is asserted separately, in
#: ``tests/unit/guide/test_capture_seams.py``.
__all__ = ["capture_pages", "scenario_resolve_url"]

#: User-facing (Polish) sentence for each reason a cached action can no longer
#: be reused. The `compile --force` hint is baked into the two identity
#: sentences only — those are the reasons that a re-freeze actually fixes;
#: every other reason needs a plain `compile` (a target change) or is a
#: transient DOM/timing condition that a re-run may simply not repeat.
_REUSE_REASON_PL = {
    "not_found": "celu nie ma na stronie",
    "not_unique": "cel pasuje do wielu elementów",
    "not_visible": "cel jest niewidoczny",
    "not_enabled": "cel jest nieaktywny",
    "not_editable": "cel nie przyjmuje tekstu",
    "incompatible_type": "typ elementu nie pasuje do akcji",
    "not_select": "cel nie jest natywnym <select>",
    "option_missing": "wybrany `<select>` nie ma żądanej opcji",
    "unsupported_action": "akcja nieobsługiwana przez walidację",
    "dom_changed": "strona zmieniła się w trakcie sprawdzania",
    "identity_mismatch": "niezgodna tożsamość — uruchom `compile --force`",
    "identity_missing": "wpis bez zamrożonej tożsamości — uruchom `compile --force`",
    "no_wait_state": "wpis oczekiwania bez stanu — uruchom `compile`",
    "wait_ambiguous": "oczekiwanie pasuje do wielu elementów",
    "sensitive_target": "cel wygląda na pole wrażliwe — `teach` go nie wypełni",
}

#: One phase per page kind :func:`classify` produces. ``action`` is the default
#: rather than an entry, which is the shape the `if`-ladder this replaced had:
#: everything that fell past the named kinds was an action step.
_PAGES: dict[str, Callable[[_StepRun], Awaitable[None]]] = {
    "gate": _gate_page,
    "navigate": _navigate_page,
    "slide": _slide_page,
    "text": _text_page,
    "scroll": _scroll_page,
    "wait": _wait_page,
}


async def _reject_unusable_target(run: _StepRun) -> None:
    """Refuse a frozen target the page no longer supports, in the guide's words."""

    cap, step = run.capture, run.step
    if step.optional or run.act == "waitFor":
        return
    # The one caller that can answer "which option?" — the label lives in
    # the scenario step, never in the sidecar. Handing it over is what lets
    # validation reject a dropdown that lost the option, instead of leaving
    # the miss to `select_option`'s 15s timeout.
    wanted_option = step.select.option if run.act == "select" and step.select else None
    reason = await reuse_failure(cap.recorder.frame, run.action, option=wanted_option)
    if reason is None:
        return
    if reason == "not_visible" and run.act == "select" and step.select is not None:
        # `not_visible` is a sentence shared with click/hover/type, but for a
        # `select` it has exactly one cause: `validate_compile_time`'s select arm
        # reaches it only through `user_visible_control() is None`, i.e. the page
        # hid the control and nothing visible stands in for it. The recorder
        # already words that situation for the render; asking it here is what
        # keeps the guide from growing a second, vaguer wording of its own.
        diagnosis = await cap.recorder.diagnose_select(run.action.target, step.select.option)
        raise GuideError(run.banner(str(diagnosis)))
    raise GuideError(run.banner(_REUSE_REASON_PL.get(reason, reason)))


def _append_step_page(run: _StepRun) -> None:
    """Build this step's page — and, in the same expression, hand the trail on."""

    cap = run.capture
    bounds = (float(run.size[0]), float(run.size[1]))
    cap.pages.append(
        GuidePage(
            kind="step",
            screenshot=run.shot,
            text=page_text(run.step),
            heading=None,
            annotations=annotations_for(
                run.act,
                # Reads the pair this page's arrow starts *from* and adopts this
                # page's own, in one expression: `annotations_for` needs the
                # previous target's shape, and there is no statement boundary
                # here for a later edit to slip the overwrite into.
                **cap.trail.advance(
                    # The next step's arrow starts where this one left the
                    # reader's eye — on the option row, when a list was opened.
                    cursor=run.row_center if run.row_center is not None else run.center,
                    shape=cursor_shape(
                        run.act,
                        box=run.box,
                        row_box=run.row_box,
                        mark=run.mark,
                        bounds=bounds,
                    ),
                ),
                center=run.center,
                box=run.box,
                row_box=run.row_box,
                row_center=run.row_center,
                mark=run.mark,
                bounds=bounds,
            ),
            screenshot_size=run.size,
        )
    )


async def _action_page(run: _StepRun) -> None:
    """click / hover / type / select / highlight — dispatch on the frozen action."""

    cap = run.capture
    if not isinstance(run.action, CachedAction):
        if run.step.optional:
            cap.note(run, "pomijam: cel nieobecny")
            return  # optional branch never compiled -> skip page
        raise RuntimeError(run.banner("nierozwiązana akcja obowiązkowa"))
    await _reject_unusable_target(run)
    if not await _approach_target(run):
        return
    if not await _FRAMES.get(run.act, _click_or_hover_frame)(run):
        return
    await cap.recorder.apply_readiness(run.action.expect)
    _append_step_page(run)


async def capture_pages(
    scenario,
    compiled,
    page: Page,
    recorder: Recorder,
    shots_dir: Path,
    *,
    timeout: float,
    verbose: bool = False,
    pause_on_error: bool = False,
    sensitive_values: Iterable[str] = (),
    selects: Selects | None = None,
) -> list[GuidePage]:
    """Replay the compiled scenario, keeping one annotated frame per step.

    ``selects`` is the DOM select shim's controller for this browser context, or
    ``None`` under ``config.selects.mode: native`` (where no widget is injected)
    — the guide's half of the readiness barrier compile and render also take.
    See :meth:`~guidebot_recorder.guide.replay._Capture.await_selects_ready` for
    where it is taken and why.
    """

    cap = _Capture(
        scenario=scenario,
        page=page,
        recorder=recorder,
        shots_dir=shots_dir,
        flat=scenario.flat_steps(),
        timeout=timeout,
        verbose=verbose,
        pause_on_error=pause_on_error,
        sensitive_values=sensitive_values,
        selects=selects,
    )
    # Wrapping the iterator (rather than `bar.update(1)` as render does) is what
    # keeps the count honest here: this loop leaves through a dozen early returns
    # — skipped branches, absent optional targets, page-less steps — and each one
    # would need its own update call to stay in step.
    steps = tqdm(
        list(zip(cap.flat, compiled.actions, strict=True)),
        desc="guide",
        unit="krok",
        disable=not verbose,
    )
    for index, (entry, action) in enumerate(steps):
        if cap.skipping(entry):
            continue
        run = _StepRun(capture=cap, index=index, entry=entry, action=action, kind=classify(entry))
        if verbose:
            # `tqdm.write` instead of `print`: a bare print tears the bar apart.
            tqdm.write(f"[{index + 1}/{len(cap.flat)}] {run.kind}")
        try:
            await _PAGES.get(run.kind, _action_page)(run)
        except Exception as exc:
            if pause_on_error:
                await pause_for_inspection(
                    page,
                    "guide",
                    index,
                    run.kind,
                    exc,
                    sensitive_values,
                    total=len(cap.flat),
                    location=entry.location,
                    source=scenario.source,
                )
            raise

    return cap.pages
