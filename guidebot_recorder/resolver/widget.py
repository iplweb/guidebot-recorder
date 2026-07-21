"""Find the control a viewer actually sees for a given ``<select>``.

Some pages hide the real ``<select>`` and render their own dropdown widget in
its place: select2 clips the original to a 1x1 px box, Tom Select sets
``display: none`` on it. Two callers both need to answer the same question —
"what does the person watching the recording actually see for this select?" —
``validate_compile_time`` (:mod:`guidebot_recorder.resolver.validate`) at
compile time, and the render choreography when it drives the step. This
module is the single implementation both consult, so the answer can never
drift between compile and render.
"""

from __future__ import annotations

from playwright.async_api import ElementHandle, JSHandle, Locator

from guidebot_recorder.selects.visibility import select_shape

#: (el) => Element | null
#:
#: Given a hidden ``<select>``, resolve the on-page widget standing in for
#: it, trying each of the following in order and stopping at the first hit:
#:
#: 1. ``aria-controls`` / ``aria-owns`` — the select explicitly names the
#:    element it drives; this is the strongest signal because it is an
#:    intentional annotation, not a heuristic.
#: 2. an element whose ``aria-labelledby`` / ``aria-describedby`` names this
#:    select's ``id`` — a back-reference in the other direction, still an
#:    explicit accessibility relationship.
#: 3. the nearest *following* element sibling with a non-empty bounding box —
#:    library-hidden selects (select2, Tom Select, Chosen) are conventionally
#:    left in place and immediately followed by the widget replacing them, so
#:    this is a reasonable structural fallback once no explicit relationship
#:    exists. "Following" (not preceding) matches how these libraries inject
#:    their replacement markup. This step skips this branch's own shim
#:    elements (``[data-guidebot-select-button]``, ``[data-guidebot-select-list]``,
#:    and anything nested inside either) — a shim element is never, by
#:    construction, the page's own widget, and shim overlays live at
#:    ``<body>`` alongside the selects they replace, so a *different*
#:    select's shim element can easily land as this select's nearest
#:    following sibling.
#: 4. otherwise ``null`` — the caller has exhausted every signal and must
#:    treat the select as having no visible stand-in.
ASSOCIATED_CONTROL_JS = """
(el) => {
  // Deliberately *not* the shared "is this select enhanced?" predicate: this
  // asks a different question of a different element. The predicate decides
  // whether a <select> is still the viewer's control (8x8 px, marker classes —
  // rules about how widget libraries hide an original). This only rejects a
  // sibling with no box at all, so a candidate widget is not disqualified for
  // being small.
  const hasBox = (node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const isShimElement = (node) =>
    node.closest("[data-guidebot-select-button], [data-guidebot-select-list]") !== null;

  const byIdRefList = (attr) => {
    const value = el.getAttribute(attr);
    if (!value) return null;
    for (const id of value.trim().split(/\\s+/)) {
      const target = el.ownerDocument.getElementById(id);
      if (target) return target;
    }
    return null;
  };

  const controlled = byIdRefList("aria-controls") || byIdRefList("aria-owns");
  if (controlled) return controlled;

  const selectId = el.id;
  if (selectId) {
    const escaped = CSS.escape(selectId);
    const referrer = el.ownerDocument.querySelector(
      `[aria-labelledby~="${escaped}"], [aria-describedby~="${escaped}"]`
    );
    if (referrer) return referrer;
  }

  for (
    let sibling = el.nextElementSibling;
    sibling;
    sibling = sibling.nextElementSibling
  ) {
    if (isShimElement(sibling)) continue;
    if (hasBox(sibling)) return sibling;
  }

  return null;
}
"""


async def _handle_to_element(handle: JSHandle) -> ElementHandle | None:
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


async def associated_control(locator: Locator) -> ElementHandle | None:
    """Resolve the on-page widget standing in for a hidden ``<select>``.

    Applies :data:`ASSOCIATED_CONTROL_JS` to the locator's single matching
    element. Returns ``None`` when none of the four steps finds anything —
    the caller (validation, or the render choreography) decides what that
    means for its own step.
    """

    handle = await locator.evaluate_handle(ASSOCIATED_CONTROL_JS)
    return await _handle_to_element(handle)


async def user_visible_control(locator: Locator) -> ElementHandle | None:
    """Return the control the viewer actually sees for this ``<select>``.

    Tries, in order:

    1. the select itself, if it still has a control the viewer could point at —
       the ``visible`` half of the one shared predicate
       (:func:`guidebot_recorder.selects.select_shape`), never Playwright's
       ``is_visible()``. The two disagree on exactly the case this function
       exists for: select2 leaves its original in place clipped to 1x1 px, which
       Playwright calls visible and the shim calls hidden. Answering it here in
       Playwright's terms made validation accept a control the render
       choreography then refused to drive;
    2. the widget resolved by :func:`associated_control`, for pages that
       enhance their own selects (select2, Tom Select, Chosen).

    The predicate's *other* half, ``markerClass``, is deliberately not consulted
    here. It answers "should the shim touch this control", not "does the viewer
    see it" — a full-size ``<select class="select2-hidden-accessible">`` is on
    screen and clickable, so it validates, and the render then tells the author
    to reach for ``mode: native``. Rejecting it here would make that advice
    impossible to follow: the step could not compile under either mode.

    A shimmed select needs no step of its own: the shim only ever takes on a
    select this predicate calls visible, so step 1 always answers for it — and
    it is the ``<select>``, not the shim button, that is the click target on
    camera (the button is ``pointer-events: none``).

    Returns ``None`` when nothing in that list is visible — nothing the user
    could see qualifies as "the control".
    """

    if (await select_shape(locator))["visible"]:
        return await locator.element_handle()

    control = await associated_control(locator)
    if control is not None and await control.is_visible():
        return control

    return None
