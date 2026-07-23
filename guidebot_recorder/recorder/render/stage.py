"""What is on screen right now: the pages, the injected layers, the popup, the card.

The second of the three lifetimes ``run_render`` used to interleave. A
:class:`_Stage` exists only while a browser context does, and every question the
render loop asks about "the picture" is a method on it: which page is live, what
should be painted on it, whether the slide card is still the thing the narration
describes, which pages fell outside the one-main-plus-one-popup contract.

**The registration order of the init scripts is a contract, and this module is
where it is kept.** ``cursor.js``, ``slide.js`` and ``desktop.js`` each decide
their role by reading the *real* ``window.top``; ``chrome.js`` is what shadows it
(frame-bust neutralization). A layer registered after ``chrome.js`` reads the
shadowed ``top``, misidentifies as the top window, and mounts a duplicate cursor
or desktop *inside* the framed site — a defect that is invisible in every test
that only checks lengths and only shows up in the finished film. So the whole
registration is one function, :func:`_install_page_scripts`, whose body *is* the
order, and a layer that is added to it without being declared in
:data:`_ROLE_GATED_LAYERS` raises at render start instead of mounting twice.
``test_render.py`` asserts the resulting call order, ``DesktopOverlay`` included.

Two test seams live here because their constructors do: ``Overlay`` and
``SlideOverlay`` are name-imported, so a patch on *this* module is what has to
reach them. ``Recorder``'s constructor is in
:mod:`~guidebot_recorder.recorder.render.loop` and ``DesktopOverlay`` is patched
on its own class, not through a render module.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Frame, Page, Video

from guidebot_recorder.chrome import SHELL_URL, Chrome
from guidebot_recorder.chrome.framing import install_framing
from guidebot_recorder.desktop import DesktopOverlay
from guidebot_recorder.models.config import Config
from guidebot_recorder.overlay.overlay import Overlay
from guidebot_recorder.recorder.session import ensure_session
from guidebot_recorder.selects import Selects, install_selects
from guidebot_recorder.slide import SlideOverlay

from .errors import RenderError
from .pages import _active_page, _expect_chrome
from .plan import _RenderPlan
from .popup_crop import _settle_popup_content_box
from .popup_detect import _POPUP_REQUEST_SCRIPT
from .popup_session import _PageObservation, _PopupSession, _sync_popup_close, _unexpected_pages
from .visuals import _ensure_visuals, _prime_visuals

#: A slide card's on-screen content, as consumed by ``SlideOverlay.show``/``.ensure``.
Card = dict[str, str | None]

#: The role-gated init scripts, in the ONE order that works — see the module
#: docstring. Declared separately from the dict that creates them so that adding a
#: fourth overlay without thinking about ``chrome.js`` is a loud failure at render
#: start rather than a duplicate cursor inside the site iframe.
_ROLE_GATED_LAYERS = ("cursor", "slide", "desktop")


def _note_closed(observed: _PageObservation, _page: Page) -> None:
    """Record when a page closed, first close wins.

    Module-level, bound per page with :func:`functools.partial`, rather than a
    closure created inside the observer: the record is the only thing it needs.
    """

    if observed.closed_at is None:
        observed.closed_at = time.monotonic()


@dataclass(frozen=True, slots=True)
class _Layers:
    """The injected layers, as :func:`_install_page_scripts` leaves them.

    Not a fourth lifetime — it is the return value of the one function that is
    allowed to register init scripts, unpacked into the :class:`_Stage` on the
    next line. It exists so that function can *be* the ordering contract without
    also needing a page that does not exist yet.
    """

    overlay: Overlay
    slide: SlideOverlay
    desktop: DesktopOverlay
    selects: Selects | None
    chrome: Chrome | None
    bare_popups: bool


# NOT ``slots=True``, unlike every other record in this package: ``_Stage.observe``
# is handed to ``BrowserContext.on("page", ...)`` as a BOUND METHOD, and
# Playwright's event plumbing memoises its wrapper by setting an attribute on the
# method's ``__self__`` — which a slotted instance rejects with a bare
# ``AttributeError`` from inside the event dispatcher.
@dataclass
class _Stage:
    """The live picture: which pages exist, what is painted, what owns the screen."""

    context: BrowserContext
    overlay: Overlay
    slide: SlideOverlay
    desktop: DesktopOverlay
    selects: Selects | None
    chrome: Chrome | None
    bare_popups: bool
    observed_pages: dict[Page, _PageObservation] = field(default_factory=dict)
    site_frame: Frame | None = None
    anchor: float = 0.0
    card: Card | None = None
    """The slide card that currently owns the screen, or None when the page does.

    One variable, not a ``(bool, payload)`` pair: the two halves were written
    together at every site and the code asserted they agreed, so the only thing a
    pair could express was a desync.
    """
    popup: _PopupSession | None = None
    popup_open_at_end: bool = False
    page: Page = field(init=False, repr=False)
    """The main window. Assigned by :func:`_open_stage` the moment it is opened.

    Not an ``__init__`` argument, and deliberately not ``None``-able: the stage
    has to exist before ``context.new_page()`` so its page observer can be
    registered on the context first, and the four lines between the two are the
    only place where reading this is a bug — where it raises ``AttributeError``
    instead of quietly handing out a ``None``.
    """
    video: Video = field(init=False, repr=False)
    """The main window's recording. Assigned with :attr:`page`."""

    # -- what should be on screen ------------------------------------------- #

    @property
    def active_page(self) -> Page:
        return _active_page(self.page, self.popup)

    @property
    def expect_chrome(self) -> bool:
        """Whether the legacy in-DOM bar is expected on an ordinary page here."""

        return _expect_chrome(self.chrome, self.bare_popups)

    def expects_bar(self, pg: Page) -> bool:
        """Same question, answered for one specific page.

        A real ``target="_blank"`` tab carries the legacy bar even when the
        context-wide script is bare, so the popup answers for itself.
        """

        if self.popup is not None and pg is self.popup.page:
            return self.popup.wants_bar
        return self.expect_chrome

    async def ensure_visuals(self, pg: Page, *, expect_chrome: bool | None = None) -> None:
        await _ensure_visuals(pg, self.overlay, self.chrome, expect_chrome=expect_chrome)

    async def chrome_hide(self, pg: Page) -> None:
        if self.chrome is not None:
            await self.chrome.hide(pg)

    async def chrome_show(self, pg: Page) -> None:
        if self.chrome is not None:
            await self.chrome.show(pg)

    # -- the slide card ------------------------------------------------------ #

    async def assert_card_alive(self, pg: Page) -> None:
        """Fail loud when a navigation destroyed the card mid-say.

        A fresh, tokenless document (``slide.token`` falsy) means the picture
        on screen is no longer the card the narration/scenario describes —
        never narrate over — or silently dismiss — the wrong picture.
        """
        if not await self.slide.token(pg):
            raise RenderError("karta slajdu zniknęła po nawigacji — narracja nad złym obrazem")

    async def ensure_card(self, pg: Page) -> None:
        """Card-aware replacement for `_ensure_visuals`: re-mount the active
        card (rebuild-from-missing only; a live card's content is untouched)
        and re-assert the hidden cursor/chrome layers.
        """
        await self.assert_card_alive(pg)
        assert self.card is not None  # only ever called on the card-active path
        await self.slide.ensure(pg, self.card)
        await self.overlay.hide(pg)
        await self.chrome_hide(pg)

    async def show_card(self, pg: Page, card: Card) -> None:
        """Paint *card* and hide the page's own layers behind it."""

        self.card = card
        await self.slide.show(pg, card)
        await self.overlay.hide(pg)
        await self.chrome_hide(pg)

    async def hide_card(self, pg: Page) -> None:
        """Take the card down, leaving the other layers as the caller found them.

        Asserts the card survived first: a card destroyed by a navigation must
        never be silently swapped out from under whatever replaced it.
        """

        await self.assert_card_alive(pg)
        await self.slide.hide(pg)
        self.card = None

    async def reveal_page(self, pg: Page) -> None:
        """Take the card down and give the page back its own visible layers."""

        await self.hide_card(pg)
        await self.overlay.show(pg)
        await self.chrome_show(pg)

    # -- page lifecycle ------------------------------------------------------ #

    def observe(self, candidate: Page) -> None:
        """Start recording a page's lifetime, and prime its visual layers."""

        if candidate in self.observed_pages:
            return
        # Bare (floating) popups carry no legacy chrome bar; nor does the main
        # window's about:blank warm-up under that flag. Prime against the cursor
        # only, or the prime loop deadlocks waiting for a bar that never mounts.
        observation = _PageObservation(
            opened_at=time.monotonic(),
            video=candidate.video,
            visual_prime=asyncio.create_task(
                _prime_visuals(
                    candidate, self.overlay, self.chrome, expect_chrome=self.expect_chrome
                )
            ),
        )
        self.observed_pages[candidate] = observation
        candidate.on("close", partial(_note_closed, observation))

    def sync_popup_close(self) -> None:
        _sync_popup_close(self.popup, self.observed_pages, self.anchor)

    def unexpected_pages(self) -> list[Page]:
        return _unexpected_pages(self.observed_pages, self.page, self.popup)

    def popup_closed_unhandled(self) -> bool:
        """A popup that went away outside an action the scenario asked for."""

        popup = self.popup
        return popup is not None and popup.page.is_closed() and not popup.close_handled


async def _install_page_scripts(context: BrowserContext, cfg: Config) -> _Layers:
    """Register every context init script. **The body of this function is the order.**

    ``cursor.js`` / ``slide.js`` / ``desktop.js`` MUST be registered before
    ``chrome.js``: inside the site iframe each of them decides its role by reading
    the real ``window.top`` (cursor.js to skip mounting a duplicate cursor,
    slide.js's ``isTop`` guard to skip installing ``window.__guidebot_slide``,
    desktop.js likewise), and ``chrome.js`` is what shadows ``top`` (frame-bust
    neutralization). A layer registered after it reads the shadowed ``top``,
    misidentifies as the top window, and mounts inside the frame.

    selects.js reads ``top`` too but is deliberately NOT part of that contract:
    its only test is ``isTop && origin === SHELL_ORIGIN``, and chrome.js shadows
    ``top`` solely inside framed documents, whose origin is never the shell's — so
    the shim reaches the same verdict on either side of chrome.js. It is
    registered here anyway, next to the overlays it sits beside; nothing
    downstream may rely on that position. See the role-gating comment at the top
    of ``selects/selects.js``.
    """

    # Independent of the role-gating order below (it only wraps ``window.open``),
    # but registered first so it wraps the *native* function on every document.
    await context.add_init_script(script=_POPUP_REQUEST_SCRIPT)
    role_gated = {
        "cursor": Overlay(cfg.cursor, cfg.viewport),
        "slide": SlideOverlay(),
        "desktop": DesktopOverlay(config={"background": cfg.desktop.color}),
    }
    # A runtime check, not a comment: a fourth overlay added here without being
    # declared in `_ROLE_GATED_LAYERS` — or declared but slipped in after
    # chrome.js — stops the render on its first line instead of quietly mounting
    # a second cursor inside the site iframe, which only the finished film shows.
    if tuple(role_gated) != _ROLE_GATED_LAYERS:
        raise RenderError(
            "kolejność rejestracji skryptów init jest kontraktem: oczekiwano "
            f"{_ROLE_GATED_LAYERS}, jest {tuple(role_gated)} — każda z tych warstw "
            "czyta prawdziwe `window.top`, a chrome.js je przesłania"
        )
    for layer in role_gated.values():
        await layer.install_context(context)
    # The DOM select shim — one of the three contexts that drive pages (spec §1),
    # and the reason the recording shows an option list at all. ``None`` under
    # ``selects.mode: native``, which keeps the page's own control.
    selects = await install_selects(context, cfg)
    # Composited popups (float or slide) render bare (no in-DOM chrome bar); the
    # compositor frames them in post. This flips the chrome.js popup-site branch
    # off and gates the fail-loud "expect chrome" checks on popup pages.
    bare_popups = cfg.popup.is_bare
    chrome = Chrome(cfg.chrome, bare_popups=bare_popups) if cfg.chrome.enabled else None
    if chrome is not None:
        await chrome.install_context(context)
        # Strip X-Frame-Options / CSP frame-ancestors so arbitrary sites frame.
        await install_framing(context, shell_origin=SHELL_URL)
    return _Layers(
        overlay=role_gated["cursor"],
        slide=role_gated["slide"],
        desktop=role_gated["desktop"],
        selects=selects,
        chrome=chrome,
        bare_popups=bare_popups,
    )


async def _open_context(
    browser: Browser,
    plan: _RenderPlan,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
) -> BrowserContext:
    """The recording context, at the configured viewport.

    The context viewport and video size stay at the configured dimensions so the
    output MP4 keeps its size and popups are geometrically untouched; the shell
    shrinks only the site iframe interior (see compile / site_viewport).
    Both settings are context-level, so a popup also records onto a
    main-viewport-sized canvas with filler around its real window. That is
    corrected in post (``compose_popup_video(popup_crop=...)``), never here:
    shrinking the recording would also shrink the main window's frame.

    Pre-recording setup: when the target declares ``config.setup`` its login
    steps were removed, so the recording context must start already logged in.
    ``ensure_session`` establishes/reuses the prepared session on separate,
    non-recording contexts *before* the context below is created, so the login can
    never reach the film (spec: "Target render").
    """

    cfg = plan.cfg
    plan.work.mkdir(parents=True, exist_ok=True)
    setup_state = (
        await ensure_session(
            browser, Path(plan.path), Path(".guidebot/sessions"), env, timeout=timeout
        )
        if cfg.setup is not None
        else None
    )
    return await browser.new_context(
        viewport={"width": cfg.viewport.width, "height": cfg.viewport.height},
        locale=cfg.locale,
        record_video_dir=str(plan.work),
        record_video_size={"width": cfg.viewport.width, "height": cfg.viewport.height},
        **({"storage_state": setup_state} if setup_state is not None else {}),
        **({"bypass_csp": True, "service_workers": "block"} if cfg.chrome.enabled else {}),
    )


async def _bootstrap_first_frame(stage: _Stage, plan: _RenderPlan) -> None:
    """Paint something, force one captured frame, then start the shared clock.

    Chromium's screencast may not emit a first frame for a pristine about:blank
    page.  A scenario can narrate for several seconds before its first navigate;
    anchoring at the Page event would then put that narration on a timeline the
    WebM never encoded.  Paint a neutral document, force one captured frame, and
    only then establish the shared narration/window clock.  The tiny warm-up is
    bounded pre-roll; it avoids losing an arbitrarily long opening narration.
    With chrome enabled the neutral document IS the shell (bar + empty iframe),
    so the recording opens on the browser chrome rather than a bare white page.
    Auto-intro (``cfg.intro.enabled``) replaces this neutral document with a
    title card instead — render-only, so ``intro.enabled=False`` keeps today's
    bootstrap byte-identical.
    """

    cfg = plan.cfg
    page = stage.page
    if stage.chrome is not None:
        stage.site_frame = await stage.chrome.install_shell(page)
    elif not cfg.intro.enabled:
        await page.set_content("<style>html,body{margin:0;background:white}</style>")
    if cfg.intro.enabled:
        await stage.show_card(
            page,
            {"title": cfg.title, "subtitle": cfg.intro.subtitle, "notes": cfg.intro.notes},
        )
    await stage.ensure_visuals(page)
    await page.screenshot()
    await page.wait_for_timeout(100)
    stage.anchor = time.monotonic()


async def _open_stage(
    browser: Browser,
    plan: _RenderPlan,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
) -> _Stage:
    """Open the recording context, inject every layer, and warm the first frame up."""

    context = await _open_context(browser, plan, env=env, timeout=timeout)
    layers = await _install_page_scripts(context, plan.cfg)
    stage = _Stage(
        context=context,
        overlay=layers.overlay,
        slide=layers.slide,
        desktop=layers.desktop,
        selects=layers.selects,
        chrome=layers.chrome,
        bare_popups=layers.bare_popups,
    )
    context.on("page", stage.observe)
    stage.page = await context.new_page()
    stage.observe(stage.page)
    stage.page.set_default_timeout(timeout * 1000)
    main_observation = stage.observed_pages[stage.page]
    if main_observation.visual_prime is not None:
        await main_observation.visual_prime
    video = stage.page.video
    if video is None:  # pragma: no cover - record_video_dir makes this invariant true
        await context.close()
        raise RenderError("Playwright nie udostępnił nagrania głównego okna")
    stage.video = video
    await _bootstrap_first_frame(stage, plan)
    return stage


async def _close_stage(stage: _Stage) -> None:
    """Settle the popup's last measurements, drain the probes, close the context.

    Runs in ``run_render``'s ``finally``, so it also runs on the way out of a
    failed render. The popup's content box is measured *here* and not later
    because this is the last moment its DOM can still answer — the context, and
    with it every page, is closed on the last line.
    """

    stage.sync_popup_close()
    popup = stage.popup
    if popup is not None and popup.closed_at is None:
        stage.popup_open_at_end = True
        popup.closed_at = max(popup.opened_at, time.monotonic() - stage.anchor)
    if popup is not None:
        # Last moment the popup's DOM can still answer: the context (and with it
        # every page) is closed a few lines below. The probe was started when the
        # popup opened, so this normally settles instantly.
        await _settle_popup_content_box(popup)
    prime_tasks = [
        observation.visual_prime
        for observation in stage.observed_pages.values()
        if observation.visual_prime is not None
    ]
    for task in prime_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*prime_tasks, return_exceptions=True)
    await stage.context.close()
