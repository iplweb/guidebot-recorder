"""Live capture pass: replay the compiled scenario and screenshot each step."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from tqdm import tqdm

from guidebot_recorder.diagnostics import step_banner
from guidebot_recorder.guide.annotate import annotations_for, target_shape
from guidebot_recorder.guide.geometry import Shape
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import GuideError, classify
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import FlatStep, WaitUntil

# Imported as a bare name (not `from ... import _debug`) so it becomes an attribute
# of this module and tests can monkeypatch it, like `reuse_failure` below.
from guidebot_recorder.recorder._debug import pause_for_inspection
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_failure

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
) -> list[GuidePage]:
    flat = scenario.flat_steps()
    actions = compiled.actions
    pages: list[GuidePage] = []
    prev_cursor: tuple[float, float] | None = None
    # The shape the cursor left, so the next arrow starts at that target's rim
    # instead of its centre. Reset wherever `prev_cursor` is: a shape from a page
    # the reader no longer sees would clip the next arrow against nothing.
    prev_shape: Shape | None = None
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
                prev_shape = None
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
                prev_shape = None
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
                    raise GuideError(banner(fs, index, _REUSE_REASON_PL.get(reason, reason)))
            try:
                res = await recorder.point(action.target, ripple=False)
            except PlaywrightError:
                if step.optional:
                    if verbose:
                        tqdm.write(banner(fs, index, "pomijam: cel nieobecny"))
                    continue
                raise
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
                try:
                    await res.locator.select_option(label=step.select.option)
                except PlaywrightError:
                    # `optional` has to cover choosing the option, not just finding
                    # the control: an optional step skips reuse validation, so a
                    # vanished option shows up here — and only here.
                    if step.optional:
                        if verbose:
                            tqdm.write(banner(fs, index, "pomijam: brak żądanej opcji"))
                        continue
                    raise
                shot, size = await _screenshot(page, shots_dir, index)  # frame AFTER selecting
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
                        prev_shape=prev_shape,
                        center=res.center,
                        box=res.box,
                        mark=mark,
                        bounds=(float(size[0]), float(size[1])),
                    ),
                    screenshot_size=size,
                )
            )
            prev_cursor = res.center
            # After the page is built, never before: `annotations_for` above needs
            # the *previous* target's shape, and this line overwrites it.
            prev_shape = target_shape(
                act, box=res.box, mark=mark, bounds=(float(size[0]), float(size[1]))
            )
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
