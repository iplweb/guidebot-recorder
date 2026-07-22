"""Everything the ``select:`` choreography asks the page, as page-side functions.

Ten expressions, each handed to Playwright's ``evaluate``/``wait_for_function``.
They live together and away from the Python that awaits them for two reasons.

**They are one language's worth of code, and it is not Python.** A reviewer
reading the choreography is following an ordering argument (approach, reveal,
click, confirm); a reviewer reading these is checking a normalisation rule
against ``selects.js``. Interleaving them made every method in the old
``recorder.py`` sit a screen further from the next.

**Three of them are composed from the others, and composition is the point.**
:data:`_OPTION_STATE_JS` embeds :data:`_OPTION_INDEX_JS`, and
:data:`_SHIM_STATE_JS` embeds
:data:`~guidebot_recorder.selects.visibility.SELECT_SHAPE_JS`, so that "which
option is this?" and "is this select enhanced?" are each stated exactly once.
This module already paid for the alternative: four definitions of "is this
select enhanced?" that disagreed. Keeping the pieces adjacent is what makes a
second copy obvious rather than plausible.

The names keep their leading underscore: they are private to the recorder, and
two tests reach in deliberately — ``tests/unit/resolver/test_validate_option_rule.py``
pins :data:`_OPTION_INDEX_JS` against the resolver's own option rule, and
``tests/unit/selects/test_selects.py`` pins :data:`_SHIM_STATE_JS` against the
shim's shared visibility predicate. Both are cross-package agreement tests, and
importing from here rather than through a re-export is what keeps them pointed
at the real definition.
"""

from __future__ import annotations

from guidebot_recorder.selects.selects import READY_TIMEOUT_MARKER
from guidebot_recorder.selects.visibility import SELECT_SHAPE_JS

#: (el) => {installed, shimmed, listbox, hidden, markerClass} — how this select
#: is presented.
#:
#: ``installed`` distinguishes "the widget ran and decided not to shim this
#: select" (so the page enhanced it itself, or ``mode: native`` is in force)
#: from "no shim layer here at all" — a bare context such as a health probe or
#: a unit-test page. Only the former says anything about drivability.
#:
#: ``listbox`` is the shim's own non-goal, read back here: ``multiple`` and
#: ``size > 1`` render their option list in the page already, so the shim never
#: touches them — which is precisely why they need their own path rather than
#: the page-widget one.
#:
#: ``hidden`` and ``markerClass`` are the two halves of the *shared* predicate
#: (``selects/visibility.js``), embedded rather than restated: ``hidden`` tells
#: "the page replaced this control" apart from "the control is on screen but
#: carries no DOM list", and ``markerClass`` names the class that caused the
#: latter, so the error message can say which one it was.
_SHIM_STATE_JS = f"""(el) => {{
  const api = window.__guidebot_selects;
  const shape = ({SELECT_SHAPE_JS})(el);
  return {{
    installed: !!api,
    shimmed: !!(api && api.isShimmed(el)),
    listbox: !!el.multiple || el.size > 1,
    hidden: !shape.visible,
    markerClass: shape.markerClass,
  }};
}}"""

#: (el) => string | null — the label of the option currently selected.
#:
#: Normalised exactly the way ``optionLabel`` normalises it in ``selects.js``,
#: which is in turn the rule Playwright's ``select_option(label=…)`` applies:
#: the ``label`` attribute when present, the option's text otherwise. Anything
#: else would let the read-back disagree with the write it is verifying.
_SELECTED_LABEL_JS = """(el) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const option = el.selectedOptions[0];
  if (!option) return null;
  return norm(option.label ? option.label : option.textContent);
}"""

#: (el, label) => number — index of the first ``<option>`` carrying ``label``.
#:
#: ``HTMLOptionElement.label`` is the ``label`` attribute when present and the
#: trimmed text otherwise, which is the same rule Playwright's
#: ``select_option(label=…)`` applies — so the option this finds is the option
#: the direct path would have set. ``el.options`` is the flattened, document
#: order list, so the index also addresses ``locator("option").nth(i)`` even
#: across ``<optgroup>`` boundaries. ``-1`` when no option matches.
_OPTION_INDEX_JS = """(el, label) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const wanted = norm(label);
  for (let i = 0; i < el.options.length; i += 1) {
    if (norm(el.options[i].label) === wanted) return i;
  }
  return -1;
}"""

#: (el, label) => {index, disabled} — everything "can this option be chosen?"
#: needs, in one round trip.
#:
#: ``index`` is :data:`_OPTION_INDEX_JS`'s answer (``-1`` when absent); ``disabled``
#: is that option's ``disabled`` flag, and ``false`` when there is no such option
#: — an absent option is not a disabled one, and the two are different verdicts
#: (see :func:`~guidebot_recorder.recorder.select.probe.require_option`).
#:
#: Composed from :data:`_OPTION_INDEX_JS` rather than repeating its loop, the same
#: way :data:`_SHIM_STATE_JS` embeds ``SELECT_SHAPE_JS``: the label rule is stated
#: once, so "which option is this?" cannot come out differently depending on which
#: of the two questions was being asked.
_OPTION_STATE_JS = f"""(el, label) => {{
  const index = ({_OPTION_INDEX_JS})(el, label);
  return {{index: index, disabled: index >= 0 && el.options[index].disabled}};
}}"""

#: (el) => void — hand this select back to the browser, durably.
#:
#: The per-step ``mode: native`` override exists for one stubborn widget in an
#: otherwise shimmed scenario, so under a global ``shim`` the select it names is
#: already shimmed: its button and DOM list sit visually on top of the real
#: control, so the cursor's approach must land on the genuine, unshimmed select —
#: otherwise the ripple would target a widget that is about to disappear out from
#: under it. Absent (bare context, ``mode: native`` globally) this is a no-op:
#: there is no shim to undo.
_PIN_NATIVE_JS = """(el) => {
  const api = window.__guidebot_selects;
  if (api && api.pinNative) {
    api.pinNative(el);
  }
}"""

#: The readiness barrier of spec §3, read straight off the page.
#:
#: Deliberately not routed through :class:`guidebot_recorder.selects.Selects`:
#: the recorder is handed a page, not the controller that installed the widget,
#: and a missing API must degrade to "nothing to wait for", not to an error.
#:
#: Bounded by the same page-side ``Promise.race`` idiom ``Selects.wait_ready``
#: uses, rejecting with the same marker: the barrier is a promise the *page*
#: settles, so awaiting it bare makes a wedged page hang the caller — precisely
#: the failure that barrier exists to prevent.
#:
#: ``settled()`` rather than ``ready``, for the reason ``Selects.wait_ready``
#: gives: ``ready`` reports the *first* classification pass and never re-arms,
#: so a select the page appended a moment ago is still unclassified when it
#: resolves — and the caller
#: (:func:`~guidebot_recorder.recorder.select.probe.await_selects_ready`) is the
#: last barrier before the step drives that select. ``ready`` remains the
#: fallback for a partial API object.
_SELECTS_READY_JS = f"""(timeoutMs) => {{
  const api = window.__guidebot_selects;
  if (!api || !api.ready) return null;
  const barrier = typeof api.settled === "function" ? api.settled() : api.ready;
  return Promise.race([
    barrier,
    new Promise((_resolve, reject) => {{
      window.setTimeout(() => reject(new Error({READY_TIMEOUT_MARKER!r})), timeoutMs);
    }}),
  ]);
}}"""

#: A short, human-readable name for a control, for error messages.
_DESCRIBE_JS = """(el) => {
  const parts = [el.tagName.toLowerCase()];
  if (el.id) parts.push("#" + el.id);
  const name = el.getAttribute("name");
  if (name) parts.push(`[name="${name}"]`);
  const label = el.getAttribute("aria-label");
  if (label) parts.push(`[aria-label="${label}"]`);
  return parts.join("");
}"""

#: Remember every element that existed *before* the list was opened, so the
#: second beat can tell the page's freshly-rendered option rows from whatever
#: already carried the same text elsewhere on the page.
#:
#: A ``WeakSet``, not a ``Set``: this is parked on ``window`` until the next
#: ``select:`` step overwrites it, and a strong set of every element in the
#: document would keep every node the page detaches in the meantime alive with
#: it. Membership is all this is ever asked, and ``WeakSet`` answers that.
_SNAPSHOT_JS = """() => {
  window.__guidebot_select_snapshot = new WeakSet(document.querySelectorAll("*"));
}"""

#: () => boolean — is the pre-click snapshot still on this document?
_HAS_SNAPSHOT_JS = "() => !!window.__guidebot_select_snapshot"

#: (label) => Element | null — the first *newly added* visible element whose
#: trimmed text is exactly the option label. ``querySelectorAll`` yields
#: document order, so the first hit is the document-order tie-break of spec §4.
#:
#: A missing snapshot yields ``null``, never a match. It used to be spelled
#: ``if (seen && seen.has(node))``, which turned the "appeared after" filter
#: into a no-op the moment beat 1 replaced the document — every node on the page
#: then qualified, up to and including ``<html>`` itself. The caller checks for
#: the snapshot explicitly and says so; this is the second lock on the same door.
_APPEARED_NODE_JS = """(label) => {
  const seen = window.__guidebot_select_snapshot;
  if (!seen) return null;
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const wanted = norm(label);
  for (const node of document.querySelectorAll("*")) {
    if (seen.has(node)) continue;
    if (norm(node.textContent) !== wanted) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    return node;
  }
  return null;
}"""
