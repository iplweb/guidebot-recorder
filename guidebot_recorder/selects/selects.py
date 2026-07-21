"""Python controller for the injected ``<select>`` shim.

Mirrors :class:`guidebot_recorder.overlay.Overlay`: a JSON config prelude is
prepended to ``selects.js`` and the result is registered as a context-level init
script, so every document — including nested iframes and popup windows — gets
the widget.

Installation belongs at *every* context that drives a page through compile or
render (spec §1); routing it through :meth:`install_context` is what keeps those
call sites from drifting apart.
"""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files

from playwright.async_api import BrowserContext

from guidebot_recorder.models.config import Config, SelectsConfig
from guidebot_recorder.selects.visibility import shape_prelude

# Floor for :meth:`Selects.wait_ready`'s deadline, in seconds. Generous next to
# the default ``settle_ms``, so it only ever fires when something is truly
# wedged.
READY_TIMEOUT = 15.0

# Mirrors ``MAX_DEFERRAL_FACTOR`` in ``selects.js``: the widget promises its
# first pass no later than this many settle windows after DOMContentLoaded.
DEFERRAL_FACTOR = 3

# Headroom on top of that promise — process start-up, the pass itself and the
# round trip through the CDP connection.
READY_MARGIN = 5.0

# The two page-side failures, spelled as markers rather than as prose: they
# travel back through Playwright as the text of a `playwright.Error`, and
# :meth:`Selects.wait_ready` matches on them to raise its own Polish
# :class:`SelectsNotReadyError`. Nothing user-facing is ever phrased here — a
# page-side `Error` surfaces in English and as the wrong exception type,
# contradicting the method's documented `Raises:`.
#
# ``READY_TIMEOUT_MARKER`` is public because ``recorder.py`` runs the same race
# for its own, unrouted read of ``ready`` (see ``_SELECTS_READY_JS`` there) and
# has to recognise the same rejection; a second spelling of the string is how
# the two would drift into one of them hanging again.
READY_TIMEOUT_MARKER = "guidebot selects ready timeout"
_API_MISSING_MARKER = "guidebot selects api unavailable"

_AWAIT_READY = f"""(timeoutMs) => {{
    const api = window.__guidebot_selects;
    if (!api || !api.ready) {{
        throw new Error({_API_MISSING_MARKER!r});
    }}
    // Race in the page as well as in Python: an unresolved `ready` must surface
    // as an error, never as an evaluate that never returns.
    return Promise.race([
        api.ready,
        new Promise((_resolve, reject) => {{
            window.setTimeout(() => reject(new Error({READY_TIMEOUT_MARKER!r})), timeoutMs);
        }}),
    ]);
}}"""


class SelectsNotReadyError(RuntimeError):
    """The widget never reported readiness for a frame within the timeout.

    Also raised when the injected API is not in the frame at all: from the
    caller's side "the widget never became usable here" is one situation with
    one fix, and it must not reach them as a raw ``playwright.Error`` in
    English.
    """


class Selects:
    """Install the DOM select shim on a Playwright browser context.

    The controller is stateless beyond its config: unlike the cursor, the widget
    keeps no position that must survive a document replacement — every document
    re-runs the init script and re-classifies its own selects.
    """

    def __init__(self, config: SelectsConfig | None = None) -> None:
        self.config = config or SelectsConfig()
        body = files("guidebot_recorder.selects").joinpath("selects.js").read_text(encoding="utf-8")
        # camelCase keys: the prelude is read by JavaScript, not by pydantic.
        # ``open_hold_ms`` stays Python-side — it paces the recorder's second
        # beat and the widget has no use for it.
        settings = {
            "mode": self.config.mode,
            "settleMs": self.config.settle_ms,
            "maxVisibleOptions": self.config.max_visible_options,
        }
        prelude = f"window.__guidebot_selects_config = {json.dumps(settings)};\n"
        # The "is this select already enhanced?" predicate is prepended, not
        # restated inside the widget: the recorder and the compile-time
        # validator evaluate the same source from Python, in contexts where this
        # widget may not be installed at all. See ``selects/visibility.py``.
        self._script = prelude + shape_prelude() + body

    @property
    def script(self) -> str:
        """The full injected script (prelude + body), for direct evaluation."""
        return self._script

    @property
    def ready_timeout(self) -> float:
        """Seconds :meth:`wait_ready` waits before declaring the widget wedged.

        Derived rather than constant: ``settle_ms`` has no upper bound, and the
        widget's own guaranteed first pass is only due ``DEFERRAL_FACTOR``
        settle windows in. A fixed 15 s ceiling is already shorter than that at
        ``settle_ms >= 5000``, which turns ordinary page churn into a spurious
        :class:`SelectsNotReadyError`.
        """
        settle_seconds = self.config.settle_ms / 1000
        return max(READY_TIMEOUT, settle_seconds * DEFERRAL_FACTOR + READY_MARGIN)

    async def install_context(self, context: BrowserContext) -> None:
        """Register the widget for every subsequently created/navigated document.

        Registration order relative to ``chrome.js`` does not change this
        widget's role gating, unlike ``cursor.js``/``slide.js``: the shim bails
        only on ``isTop and origin == SHELL_ORIGIN``, and ``chrome.js`` shadows
        ``window.top`` only inside framed documents, whose origin is never the
        shell's. See the role-gating comment at the top of ``selects.js``.
        """
        await context.add_init_script(script=self._script)

    @staticmethod
    def _frame_url(frame) -> str:
        return getattr(frame, "url", "") or "(nieznany adres)"

    def _not_ready(self, frame, timeout: float) -> SelectsNotReadyError:
        """The user-facing failure: which frame gave up, and what to try next."""
        return SelectsNotReadyError(
            f"widget select nie zgłosił gotowości w ciągu {timeout:.1f} s "
            f"dla ramki {self._frame_url(frame)}. Zwiększ selects.settleMs, jeśli "
            f"strona długo się inicjalizuje, albo ustaw selects.mode: native, aby "
            f"zrezygnować z podmiany list rozwijanych na tej stronie."
        )

    def _not_installed(self, frame) -> SelectsNotReadyError:
        """The other way the barrier fails: the script never ran in this frame."""
        return SelectsNotReadyError(
            f"widget select nie został wstrzyknięty do ramki {self._frame_url(frame)} "
            f"— skrypt nakładki nie wykonał się w tym dokumencie. Sprawdź, czy "
            f"kontekst przeglądarki powstał przez install_selects()."
        )

    async def wait_ready(self, frame, timeout: float | None = None) -> None:
        """Block until the frame's first classification pass has finished.

        Without this barrier a step could resolve or run against a page that has
        not been shimmed yet, so compile and render would see different DOM for
        the same instant.

        Bounded on purpose: waiting forever would turn any page whose widget
        never settles into a compile or render that hangs with no diagnosis. The
        default bound is :attr:`ready_timeout`, which tracks ``settle_ms``.

        Raises:
            SelectsNotReadyError: the widget did not settle within ``timeout``,
                or never ran in this frame at all.
        """
        if timeout is None:
            timeout = self.ready_timeout
        timeout_ms = max(1, int(timeout * 1000))
        try:
            # The page-side race is the primary guard; the outer wait covers the
            # case where the document itself stops running timers.
            await asyncio.wait_for(
                frame.evaluate(_AWAIT_READY, timeout_ms),
                timeout=timeout + 1.0,
            )
        except TimeoutError as exc:
            raise self._not_ready(frame, timeout) from exc
        except Exception as exc:
            # Both page-side markers are this method's own documented failure,
            # so both leave as `SelectsNotReadyError`. Anything else is a real
            # page error and belongs to the caller unchanged.
            message = str(exc)
            if _API_MISSING_MARKER in message:
                raise self._not_installed(frame) from exc
            if READY_TIMEOUT_MARKER not in message:
                raise
            raise self._not_ready(frame, timeout) from exc


async def install_selects(context: BrowserContext, cfg: Config) -> Selects | None:
    """Install the DOM select shim on a browser context that drives scenario steps.

    The single funnel for every such context — compile
    (``compile.run_compile_in_browser``), render (``render.run_render``) and
    setup replay (``session.replay_setup``) — so the three cannot drift apart and
    compile keeps freezing targets against the very DOM render later drives. It
    lives beside the widget it installs, so no phase owns it and every phase
    imports it from the same place.

    Returns the controller, whose :meth:`Selects.wait_ready` is the readiness
    barrier the caller must take before resolving a step — or ``None`` when
    ``config.selects.mode`` is ``native``: that escape hatch keeps the page's own
    control, so there is no widget to install and nothing to wait for.

    Registration order relative to ``chrome.js`` is *not* a constraint on this
    installer, unlike ``cursor.js``/``slide.js``/``desktop.js``: see
    :meth:`Selects.install_context` for why the shim's role gating survives a
    shadowed ``window.top``, and ``render.run_render`` for the contract those
    three do have.
    """

    if cfg.selects.mode == "native":
        return None
    selects = Selects(cfg.selects)
    await selects.install_context(context)
    return selects
