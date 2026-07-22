"""Public entry point for `guidebot guide`: compiled scenario -> PDF."""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Browser, Frame

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.guide.capture import capture_pages
from guidebot_recorder.guide.layout import render_html
from guidebot_recorder.guide.pdf import html_to_pdf
from guidebot_recorder.guide.prolog import GuideError, scan_for_blockers
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder._debug import scenario_sensitive_values
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario, scenario_env_references
from guidebot_recorder.selects import install_selects


async def run_guide(
    path: Path,
    out_pdf: Path,
    browser: Browser,
    *,
    env: dict | None = None,
    timeout: float = 15.0,
    verbose: bool = False,
    pause_on_error: bool = False,
) -> int:
    """Compiled scenario -> step-by-step PDF guide. Returns the page count.

    Replays the scenario with Playwright exactly like `run_render` does (same
    context/overlay/chrome-shell setup), minus video recording and TTS — and
    0×LLM (`scan_for_blockers` already rejected anything a static pass cannot
    resolve). The caller decides whether the browser is headless.
    """

    path = Path(path)
    out_pdf = Path(out_pdf)
    scenario = load_scenario(path, env)
    # Only used to redact the `--pause-on-error` message.
    sensitive_values = scenario_sensitive_values(scenario, scenario_env_references(path, env))
    cpath = compiled_path(path)
    try:
        compiled = load_compiled(cpath)
    except FileNotFoundError as exc:
        raise GuideError(f"brak pliku compiled ({cpath.name}) — uruchom `compile`") from exc
    if compiled.source != path.name:
        raise GuideError(
            f"compiled pochodzi z innego scenariusza ({compiled.source}) — uruchom `compile`"
        )
    flat = scenario.flat_steps()
    if len(compiled.actions) != len(flat):
        raise GuideError("compiled niezgodny z liczbą kroków — uruchom `compile`")
    scan_for_blockers(flat, compiled.actions)

    cfg = scenario.config
    # Same context recipe as run_render (recorder/render/_run.py), minus
    # record_video_dir/record_video_size: PDF capture never records video.
    context = await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        **({"bypass_csp": True, "service_workers": "block"} if cfg.chrome.enabled else {}),
    )
    overlay = Overlay(cfg.cursor, cfg.viewport)
    # cursor.js must be registered before chrome.js (same role-gating order as
    # run_render): chrome.js shadows window.top, and cursor.js relies on the
    # real one to decide whether to mount inside the site iframe.
    await overlay.install_context(context)
    # The DOM select shim, through the same funnel compile, render and setup
    # replay use — a `select:` page is worth reading only if the frame shows the
    # list unfurled, and that list is DOM only because this script is here.
    # `None` under `selects.mode: native`, which keeps the page's own control.
    selects = await install_selects(context, cfg)
    chrome = Chrome(cfg.chrome, bare_popups=cfg.popup.is_bare) if cfg.chrome.enabled else None
    if chrome is not None:
        await chrome.install_context(context)
        await install_framing(context, shell_origin=SHELL_URL)

    try:
        page = await context.new_page()
        page.set_default_timeout(timeout * 1000)

        site_frame: Frame | None = None
        if chrome is not None:
            site_frame = await chrome.install_shell(page)

        # Main window drives the site iframe (a Frame) when chrome is mounted;
        # otherwise the recorder drives the page directly (frame=None -> page).
        recorder = Recorder(page, overlay, frame=site_frame, type_delay_ms=None)

        shots_dir = out_pdf.parent / (out_pdf.stem + "_shots")
        pages = await capture_pages(
            scenario,
            compiled,
            page,
            recorder,
            shots_dir,
            timeout=timeout,
            verbose=verbose,
            pause_on_error=pause_on_error,
            sensitive_values=sensitive_values,
            selects=selects,
        )
    finally:
        await context.close()

    html = render_html(pages, title=cfg.title)
    await html_to_pdf(browser, html, out_pdf)
    return len(pages)
