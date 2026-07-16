"""Full-frame text-card overlay for Playwright pages.

Mirrors ``overlay/`` (synthetic cursor) and ``chrome/`` (browser bar): a DOM
overlay injected via a context-level init script and driven from Python via
``page.evaluate``.

The controller class defined in ``slide.py`` is named ``Slide`` to mirror
``Overlay``/``Chrome`` one-to-one — but ``guidebot_recorder.models.scenario.Slide``
is the unrelated *scenario step* model (the ``slide:`` command in a YAML
scenario). To avoid ambiguity at call sites, this package re-exports the
controller under the alias ``SlideOverlay``; prefer
``from guidebot_recorder.slide import SlideOverlay`` over importing the bare
``Slide`` name from ``guidebot_recorder.slide.slide``.
"""

from guidebot_recorder.slide.slide import Slide as SlideOverlay

__all__ = ["SlideOverlay"]
