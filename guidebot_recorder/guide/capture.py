"""Live capture pass: replay the compiled scenario and screenshot each step."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from guidebot_recorder.guide.annotate import annotations_for
from guidebot_recorder.guide.model import GuidePage, page_text
from guidebot_recorder.guide.prolog import GuideError, classify
from guidebot_recorder.models.action import CachedAction
from guidebot_recorder.models.scenario import WaitUntil
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.validate import reuse_is_valid


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
) -> list[GuidePage]:
    flat = scenario.flat_steps()
    actions = compiled.actions
    pages: list[GuidePage] = []
    prev_cursor: tuple[float, float] | None = None
    skipped_branch: int | None = None

    for index, (fs, action) in enumerate(zip(flat, actions, strict=True)):
        step = fs.step
        if skipped_branch is not None:
            if fs.branch == skipped_branch:
                continue
            skipped_branch = None
        kind = classify(fs)

        if kind == "gate":
            try:
                target = action.target if isinstance(action, CachedAction) else None
                if target is None:
                    skipped_branch = fs.branch
                    if verbose:
                        print(f"pomijam gałąź {fs.branch}: bramka nieobecna")
                    continue
                await recorder.wait_for(target, "visible", timeout)
            except PlaywrightError:
                skipped_branch = fs.branch  # branch element absent -> skip whole branch
                if verbose:
                    print(f"pomijam gałąź {fs.branch}: bramka nieobecna")
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
                    kind="text", screenshot=None, text=page_text(step), heading=None, annotations=[]
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
                print(f"pomijam krok {index}: oczekiwanie nierozwiązane — uruchom `compile`")
            continue

        # kind == "action": click / hover / type (dispatch on cached.action)
        if not isinstance(action, CachedAction):
            if step.optional:
                if verbose:
                    print(f"pomijam krok {index}: cel nieobecny")
                continue  # optional branch never compiled -> skip page
            raise RuntimeError(f"krok {index}: nierozwiązana akcja obowiązkowa")
        act = action.action
        if not step.optional and act != "waitFor":
            if not await reuse_is_valid(recorder.frame, action):
                raise GuideError(f"krok {index}: niezgodna tożsamość — uruchom `compile --force`")
        try:
            res = await recorder.point(action.target, ripple=False)
        except PlaywrightError:
            if step.optional:
                if verbose:
                    print(f"pomijam krok {index}: cel nieobecny")
                continue
            raise
        if act == "type":
            text = (step.enter_text.text if step.enter_text else None) or action.input_text
            if text is None:
                raise GuideError(f"krok {index}: brak zamrożonego tekstu — uruchom `compile`")
            await res.locator.fill(text)
            shot, size = await _screenshot(page, shots_dir, index)  # frame AFTER typing
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
                    act, prev_cursor=prev_cursor, center=res.center, box=res.box
                ),
                screenshot_size=size,
            )
        )
        prev_cursor = res.center

    return pages
