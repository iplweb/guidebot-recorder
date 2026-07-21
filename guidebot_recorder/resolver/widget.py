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
#:    their replacement markup.
#: 4. otherwise ``null`` — the caller has exhausted every signal and must
#:    treat the select as having no visible stand-in.
ASSOCIATED_CONTROL_JS = """
(el) => {
  const hasBox = (node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

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
    if (hasBox(sibling)) return sibling;
  }

  return null;
}
"""

#: (el) => Element | null
#:
#: Task 2's shim contract: a shimmed select carries
#: ``data-guidebot-shimmed="<uid>"``, and its stand-in button carries
#: ``data-guidebot-select-button`` plus ``data-guidebot-for="<uid>"``. Both
#: attributes are read here rather than assumed, because a select that was
#: never shimmed (or was unshimmed again, per the spec's late-hydration case)
#: simply has no ``data-guidebot-shimmed`` attribute, and this must return
#: ``null`` rather than match some unrelated button.
_SHIM_BUTTON_JS = """
(el) => {
  const uid = el.getAttribute("data-guidebot-shimmed");
  if (!uid) return null;
  const escaped = CSS.escape(uid);
  return el.ownerDocument.querySelector(
    `[data-guidebot-select-button][data-guidebot-for="${escaped}"]`
  );
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

    1. the select itself, if it is visible (the common, unshimmed case, and
       select2's 1x1-clipped original — clipping still leaves a non-empty
       box, so Playwright already considers it visible);
    2. its shim button (Task 2's overlay), if the select was hidden and
       shimmed by ``selects.js``;
    3. the widget resolved by :func:`associated_control`, for pages that
       enhance their own selects (select2, Tom Select, Chosen) without going
       through the shim at all.

    Returns ``None`` when nothing in that list is visible — nothing the user
    could see qualifies as "the control".
    """

    if await locator.is_visible():
        return await locator.element_handle()

    shim_handle = await locator.evaluate_handle(_SHIM_BUTTON_JS)
    shim_button = await _handle_to_element(shim_handle)
    if shim_button is not None and await shim_button.is_visible():
        return shim_button

    control = await associated_control(locator)
    if control is not None and await control.is_visible():
        return control

    return None
