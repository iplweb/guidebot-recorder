"""The one predicate for "is this ``<select>`` already enhanced?".

``visibility.js`` holds the rule; this module is the thin Python accessor. Three
consumers read it and no consumer restates it:

* ``selects.js``'s ``isEnhanced`` — through the ``__guidebot_select_shape``
  global that :class:`guidebot_recorder.selects.selects.Selects` installs ahead
  of the widget body;
* ``recorder/select/_js.py``'s ``_SHIM_STATE_JS`` — by embedding :data:`SELECT_SHAPE_JS`
  into its own classification pass;
* ``resolver/widget.py``'s ``user_visible_control`` — through
  :func:`select_shape`.

Keeping the source in one file rather than the answer in one runtime object is
deliberate: the recorder and the validator both run in contexts where no widget
was installed (``config.selects.mode: native``, a health probe, a unit-test
page), so they cannot depend on the global — but they must not disagree with it.
"""

from __future__ import annotations

from importlib.resources import files
from typing import TypedDict

from playwright.async_api import Locator

#: ``(el) => {visible, markerClass, enhanced}`` — see ``visibility.js``.
#:
#: A JavaScript *expression*, so it drops straight into a Playwright
#: ``evaluate`` and can be parenthesised into a larger page function.
SELECT_SHAPE_JS = (
    files("guidebot_recorder.selects").joinpath("visibility.js").read_text(encoding="utf-8")
)

#: Where :class:`Selects` parks :data:`SELECT_SHAPE_JS` for ``selects.js``.
SELECT_SHAPE_GLOBAL = "__guidebot_select_shape"


class SelectShape(TypedDict):
    """What :data:`SELECT_SHAPE_JS` returns for one ``<select>``."""

    #: the control still has a clickable box of its own (the geometric half)
    visible: bool
    #: the marker class a widget library left behind, or ``None``
    markerClass: str | None
    #: ``not visible`` or a marker class — "the page took this control over"
    enhanced: bool


def shape_prelude() -> str:
    """The statement that publishes the predicate to page scripts."""

    return f"window.{SELECT_SHAPE_GLOBAL} = {SELECT_SHAPE_JS};\n"


async def select_shape(locator: Locator) -> SelectShape:
    """Read :data:`SELECT_SHAPE_JS` for the locator's single matching element."""

    return await locator.evaluate(SELECT_SHAPE_JS)
