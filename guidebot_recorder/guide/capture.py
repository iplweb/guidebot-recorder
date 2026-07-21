"""Live capture pass: replay the compiled scenario and screenshot each step."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.guide.annotate import annotations_for
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import GuideError, classify
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import FlatStep, WaitUntil, select_mode

# Imported as a bare name (not `from ... import _debug`) so it becomes an attribute
# of this module and tests can monkeypatch it, like `reuse_failure` below.
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import (
    OPTION_MISSING,
    Recorder,
    SelectDriveError,
    SelectReveal,
)
from guidebot_recorder.resolver.validate import reuse_failure
from guidebot_recorder.selects import Selects, SelectsNotReadyError

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


def scenario_resolve_url(scenario, url: str | None) -> str:
    """Resolve a possibly-relative navigate URL against the scenario base_url.

    Mirrors ``render._resolve_url`` (render.py:1474-1478): relative URLs are
    joined onto ``config.base_url`` with ``urljoin`` only when a base is
    configured; an absolute URL, or the absence of a base, passes through
    unchanged. ``url`` is defensively allowed to be ``None`` (navigate steps
    always carry a URL in practice, but the type isn't statically guaranteed).
    """
    base = scenario.config.base_url
    if url is None:
        return base or ""
    if base and not url.startswith(("http://", "https://")):
        return urljoin(base, url)
    return url


async def _screenshot(page: Page, shots_dir: Path, index: int) -> tuple[Path, tuple[int, int]]:
    shots_dir.mkdir(parents=True, exist_ok=True)
    path = shots_dir / f"step-{index:03d}.png"
    await page.screenshot(path=str(path))
    size = page.viewport_size or {"width": 1280, "height": 720}
    return path, (size["width"], size["height"])


class _OpenListFrame:
    """The `select:` step's screenshot, taken while its option list is unfurled.

    `select:` is the one action whose frame belongs *inside* the interaction
    rather than before or after it. `click`/`hover` are photographed before the
    action and `type` after the fill, but a dropdown that is worth documenting is
    only itself for one instant: list open, option row under the cursor, nothing
    chosen yet. A frame taken after the choice shows a collapsed control that has
    silently changed value — which is the complaint this whole capture exists to
    answer.

    `Recorder.select` awaits this at exactly that instant, on every class of
    control, so everything the PDF page needs is read here: none of it survives
    the click, and the box the cursor approach measured beforehand is by then the
    *collapsed* control's, which for a page-enhanced select was never even on
    screen.
    """

    def __init__(self, page: Page, shots_dir: Path, index: int) -> None:
        self._page = page
        self._shots_dir = shots_dir
        self._index = index
        self.shot: Path | None = None
        self.size: tuple[int, int] | None = None
        self.reveal: SelectReveal | None = None

    async def __call__(self, reveal: SelectReveal) -> None:
        self.reveal = reveal
        self.shot, self.size = await _screenshot(self._page, self._shots_dir, self._index)


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
    See :func:`await_selects_ready` below for where it is taken and why.
    """

    flat = scenario.flat_steps()
    actions = compiled.actions
    pages: list[GuidePage] = []
    prev_cursor: tuple[float, float] | None = None
    skipped_branch: int | None = None

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

    async def await_selects_ready(entry: FlatStep, entry_index: int) -> None:
        """Take the shim's readiness barrier, blaming the step that was running.

        Taken twice: after every navigation, because the first frame of a new
        document is photographed immediately and must show the same DOM compile
        resolved against; and again before a ``select:`` step drives its control.
        The second is not covered by the first — ``wait_ready`` answers "is a
        pass owed *right now*", so a navigation-time answer says nothing about a
        select the page grew three steps later, and a select the pending pass has
        not reached yet is a bare ``<select>`` with no DOM list to unfurl.

        ``Recorder.select`` takes a barrier of its own, but that one is
        explicitly the *backstop* for a direct caller: it reads the page API
        itself on a flat, generous bound, knowing nothing of ``settle_ms``. The
        guide is a production caller like compile and render, so like them it
        takes the controller's bounded barrier first — which is also what turns
        a wedged widget into this step's banner (`plik:linia` plus the YAML
        fragment) instead of a bare exception from inside the recorder.
        """

        if selects is None:
            return
        try:
            await selects.wait_ready(recorder.frame)
        except SelectsNotReadyError as exc:
            raise GuideError(banner(entry, entry_index, str(exc))) from exc

    # Wrapping the iterator (rather than `bar.update(1)` as render does) is what
    # keeps the count honest here: this loop leaves through a dozen `continue`s
    # — skipped branches, absent optional targets, page-less steps — and each
    # one would need its own update call to stay in step.
    steps = tqdm(
        list(zip(flat, actions, strict=True)),
        desc="guide",
        unit="krok",
        disable=not verbose,
    )
    for index, (fs, action) in enumerate(steps):
        step = fs.step
        if skipped_branch is not None:
            if fs.branch == skipped_branch:
                continue
            skipped_branch = None
        kind = classify(fs)
        if verbose:
            # `tqdm.write` instead of `print`: a bare print tears the bar apart.
            tqdm.write(f"[{index + 1}/{len(flat)}] {kind}")
        try:
            if kind == "gate":
                try:
                    target = action.target if isinstance(action, CachedAction) else None
                    if target is None:
                        skipped_branch = fs.branch
                        if verbose:
                            tqdm.write(f"pomijam gałąź {fs.branch}: bramka nieobecna")
                        continue
                    # action is a CachedAction here (target came from it above); mirror the
                    # non-gate wait branch's state fallback and use the gate's own timeout.
                    state = action.state or "visible"
                    gate_timeout = (
                        step.wait.timeout if isinstance(step.wait, WaitUntil) else timeout
                    )
                    await recorder.wait_for(target, state, gate_timeout)
                except PlaywrightError:
                    skipped_branch = fs.branch  # branch element absent -> skip whole branch
                    if verbose:
                        tqdm.write(f"pomijam gałąź {fs.branch}: bramka nieobecna")
                continue

            if kind == "navigate":
                url = scenario_resolve_url(scenario, step.navigate_url())
                await recorder.navigate(url)
                await await_selects_ready(fs, index)
                shot, size = await _screenshot(page, shots_dir, index)
                pages.append(
                    GuidePage(
                        kind="navigate",
                        screenshot=shot,
                        text=page_text(step),
                        heading=f"Otwórz adres: {url}",
                        annotations=[],
                        screenshot_size=size,
                    )
                )
                prev_cursor = None
                continue

            if kind == "slide":
                s = step.slide
                pages.append(
                    GuidePage(
                        kind="slide",
                        screenshot=None,
                        text=s.subtitle or s.notes or "",
                        heading=s.title,
                        annotations=[],
                    )
                )
                continue

            if kind == "text":
                pages.append(
                    GuidePage(
                        kind="text",
                        screenshot=None,
                        text=page_text(step),
                        heading=None,
                        annotations=[],
                    )
                )
                continue

            if kind == "scroll":
                await recorder.scroll(step.scroll_config())
                prev_cursor = None
                text = page_text(step)
                if not text:
                    continue
                shot, size = await _screenshot(page, shots_dir, index)
                pages.append(
                    GuidePage(
                        kind="step",
                        screenshot=shot,
                        text=text,
                        heading=None,
                        annotations=[],
                        screenshot_size=size,
                    )
                )
                continue

            if kind == "wait":
                if isinstance(step.wait, int | float):
                    await recorder.wait_seconds(float(step.wait))
                    continue
                if isinstance(action, CachedAction) and action.action == "waitFor":
                    timeout_wait = step.wait.timeout if isinstance(step.wait, WaitUntil) else 10.0
                    await recorder.wait_for(action.target, action.state or "visible", timeout_wait)
                elif verbose and not step.optional:
                    tqdm.write(
                        banner(fs, index, "pomijam: oczekiwanie nierozwiązane — uruchom `compile`")
                    )
                continue

            # kind == "action": click / hover / type / select / highlight
            # (dispatch on cached.action)
            if not isinstance(action, CachedAction):
                if step.optional:
                    if verbose:
                        tqdm.write(banner(fs, index, "pomijam: cel nieobecny"))
                    continue  # optional branch never compiled -> skip page
                raise RuntimeError(banner(fs, index, "nierozwiązana akcja obowiązkowa"))
            act = action.action
            # The one caller that can answer "which option?" — the label lives in
            # the scenario step, never in the sidecar. Handing it over is what lets
            # validation reject a dropdown that lost the option, instead of leaving
            # the miss to `select_option`'s 15s timeout.
            wanted_option = step.select.option if act == "select" and step.select else None
            if not step.optional and act != "waitFor":
                reason = await reuse_failure(recorder.frame, action, option=wanted_option)
                if reason is not None:
                    if reason == "not_visible" and act == "select" and step.select is not None:
                        # `not_visible` is a sentence shared with click/hover/type,
                        # but for a `select` it has exactly one cause:
                        # `validate_compile_time`'s select arm reaches it only
                        # through `user_visible_control() is None`, i.e. the page
                        # hid the control and nothing visible stands in for it.
                        # The recorder already words that situation for the
                        # render; asking it here is what keeps the guide from
                        # growing a second, vaguer wording of its own.
                        raise GuideError(
                            banner(
                                fs,
                                index,
                                str(
                                    await recorder.diagnose_select(
                                        action.target, step.select.option
                                    )
                                ),
                            )
                        )
                    raise GuideError(banner(fs, index, _REUSE_REASON_PL.get(reason, reason)))
            try:
                # Doubles as the "is the target here at all?" probe, which is why
                # `select` goes through it too even though its choreography
                # approaches the control again: only a *resolution* failure here
                # means an optional step's target is absent, and widening the
                # `except` below to cover the choreography would start reading a
                # failed click as one.
                res = await recorder.point(action.target, ripple=False)
            except PlaywrightError:
                if step.optional:
                    if verbose:
                        tqdm.write(banner(fs, index, "pomijam: cel nieobecny"))
                    continue
                raise
            # Where this step's marks go. For every action but `select` that is
            # the control the cursor just approached; `select` re-derives it from
            # the open list, because by the time its frame is taken `res.box` is
            # the *collapsed* control's and stale.
            center, box = res.center, res.box
            row_box: dict | None = None
            row_center: tuple[float, float] | None = None
            mark = None
            if act == "type":
                text = (step.enter_text.text if step.enter_text else None) or action.input_text
                if text is None:
                    raise GuideError(
                        banner(fs, index, "brak zamrożonego tekstu — uruchom `compile`")
                    )
                await res.locator.fill(text)
                shot, size = await _screenshot(page, shots_dir, index)  # frame AFTER typing
            elif act == "select":
                if step.select is None:
                    # Plain `compile`, not `--force`: editing the step changed its
                    # command_kind, so the fingerprint already fails to match and the
                    # entry is re-resolved on its own. `--force` would pointlessly
                    # re-freeze every other step in the scenario.
                    raise GuideError(
                        banner(
                            fs,
                            index,
                            "sidecar mówi `select`, a krok scenariusza nim nie jest "
                            "— uruchom `compile`",
                        )
                    )
                # The step that actually drives a `<select>`, so the barrier is
                # taken again here — see `await_selects_ready`.
                await await_selects_ready(fs, index)
                # Named `still`, not `frame`: in this codebase `frame` means a
                # Playwright frame everywhere else.
                still = _OpenListFrame(page, shots_dir, index)
                try:
                    # `ripple=False` for the same reason `point` above uses it:
                    # a still capture wants a clean frame, not a click ring
                    # frozen mid-animation.
                    await recorder.select(
                        action.target,
                        step.select.option,
                        native=select_mode(step, scenario.config) == "native",
                        ripple=False,
                        on_revealed=still,
                    )
                except SelectDriveError as exc:
                    # `optional` has to cover choosing the option, not just
                    # finding the control: an optional step skips reuse
                    # validation, so a vanished option shows up here — and only
                    # here. Narrowed to that one reason on purpose: every other
                    # `SelectDriveError` (a click that did not take, a widget
                    # with nothing to unfurl, a shim removed mid-step) says the
                    # step *is* broken, and skipping those would hide exactly
                    # the failures this choreography was written to surface.
                    if step.optional and exc.reason == OPTION_MISSING:
                        if verbose:
                            tqdm.write(banner(fs, index, "pomijam: brak żądanej opcji"))
                        continue
                    # No silent fallback to `select_option`: it would restore
                    # exactly the invisible value change this capture exists to
                    # remove, and the PDF would look fine while being useless.
                    raise GuideError(banner(fs, index, str(exc))) from exc
                except SelectsNotReadyError as exc:
                    raise GuideError(banner(fs, index, str(exc))) from exc
                if still.shot is None or still.size is None or still.reveal is None:
                    raise GuideError(
                        banner(fs, index, "krok `select` nie oddał kadru z rozwiniętą listą")
                    )
                shot, size = still.shot, still.size
                center, box = still.reveal.control_center, still.reveal.control_box
                row_box, row_center = still.reveal.row_box, still.reveal.row_center
            elif act == "highlight":
                if step.highlight is None:
                    raise GuideError(
                        banner(
                            fs,
                            index,
                            "sidecar mówi `highlight`, a krok scenariusza nim nie jest "
                            "— uruchom `compile --force`",
                        )
                    )
                # Deliberately no action on the element: `highlight` never touches
                # the page, and the `else` below would click it. The mark itself is
                # drawn onto the page by the annotation, not by the browser.
                mark = step.highlight.resolved(scenario.config.highlight)
                shot, size = await _screenshot(page, shots_dir, index)
            else:
                shot, size = await _screenshot(page, shots_dir, index)  # frame BEFORE click/hover
                if act == "hover":
                    await res.locator.hover()
                else:
                    await res.locator.click()
            await recorder.apply_readiness(action.expect)
            pages.append(
                GuidePage(
                    kind="step",
                    screenshot=shot,
                    text=page_text(step),
                    heading=None,
                    annotations=annotations_for(
                        act,
                        prev_cursor=prev_cursor,
                        center=center,
                        box=box,
                        row_box=row_box,
                        row_center=row_center,
                        mark=mark,
                        bounds=(float(size[0]), float(size[1])),
                    ),
                    screenshot_size=size,
                )
            )
            # The next step's arrow starts where this one left the reader's eye —
            # on the option row, when a list was opened.
            prev_cursor = row_center if row_center is not None else center
        except Exception as exc:
            if pause_on_error:
                await pause_for_inspection(
                    page,
                    "guide",
                    index,
                    kind,
                    exc,
                    sensitive_values,
                    total=len(flat),
                    location=fs.location,
                    source=scenario.source,
                )
            raise

    return pages
