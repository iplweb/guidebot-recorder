/**
 * (el) => {visible, markerClass, enhanced} — how a `<select>` presents itself.
 *
 * The single answer to "is this control still the viewer's, or has the page
 * taken it over?". Four modules used to answer it four different ways, and the
 * drift between them is what produced two regressions on this branch: a
 * select2 original clipped to 1x1 px was "the control the viewer sees" to
 * validation while the shim called it enhanced and invisible, and a select
 * carrying a marker class was diagnosed purely off geometry, so the message
 * never mentioned the class that actually caused it.
 *
 * A bare expression on purpose, not an IIFE or a module: the JS side installs
 * it once as `window.__guidebot_select_shape` (see `Selects._script`) while the
 * Python side evaluates this very source inline — the recorder is handed a page,
 * not the controller that installed the widget, and `config.selects.mode:
 * native` installs no widget at all. Both therefore need an answer that does
 * not depend on the shim being present, and it has to be the same answer.
 *
 * `visible` is the geometric half: what "the page hid this control" means.
 * `markerClass` is the belt-and-braces half — the class name, not just a
 * boolean, so an error message can name it. The geometric test is the primary
 * signal because it is library-agnostic (select2 clips the original to 1x1 px,
 * Tom Select uses display:none, Chosen hides it too); the marker classes catch
 * the rest. `enhanced` is the disjunction, and it is the question every caller
 * outside this file actually asks.
 */
(el) => {
  const MARKER_CLASSES = ["select2-hidden-accessible", "tomselected", "chosen-select"];
  const computed = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  // 8x8 rather than "any box at all": a control the viewer is meant to click
  // is never smaller than that, and select2's 1x1 px original must not read as
  // on screen just because Playwright's `is_visible()` would say so.
  const visible = !(
    computed.display === "none" ||
    computed.visibility === "hidden" ||
    rect.width < 8 ||
    rect.height < 8
  );
  let markerClass = null;
  for (const name of MARKER_CLASSES) {
    if (el.classList.contains(name)) {
      markerClass = name;
      break;
    }
  }
  return {
    visible: visible,
    markerClass: markerClass,
    enhanced: !visible || markerClass !== null,
  };
}
