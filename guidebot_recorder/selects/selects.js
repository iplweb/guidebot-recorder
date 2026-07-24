(() => {
  "use strict";

  // --- Role gating -----------------------------------------------------------
  // Unlike cursor.js/slide.js, the shim belongs to the *site*, not to the top
  // window: it must run inside the framed site (isTop === false) and inside
  // top-level popup site documents, but never in the shell, which holds no page
  // content — only the address bar and the site iframe.
  //
  // The registration order relative to chrome.js (the role-gating contract in
  // `render.run_render`) is real for those overlays, but NOT for this file: the
  // only test here is `isTop && origin === SHELL_ORIGIN`, and chrome.js shadows
  // `top` only in its framed branch (chrome.js:18-27), where the origin is never
  // the shell's. Reading `top` before chrome.js can shadow it is therefore
  // defensive, not load-bearing — the outcome is the same in either order.
  // `Selects.install_context` and `render.run_render` say the same on the
  // Python side; if one of the three ever changes, all three must.
  const SHELL_ORIGIN = "https://guidebot.shell";
  const isTop = window === window.top;
  let documentOrigin = "";
  try {
    documentOrigin = window.location.origin;
  } catch (_error) {
    documentOrigin = "";
  }
  if (isTop && documentOrigin === SHELL_ORIGIN) {
    return;
  }

  const API_KEY = "__guidebot_selects";
  const API_VERSION = 1;

  const MARKER_ATTRIBUTE = "data-guidebot-shimmed";
  // "This select is the per-step `mode: native` escape hatch — hands off."
  // Durable on purpose: dropping the shim alone would last only until the next
  // classification pass shimmed the select right back, under the recorder's feet.
  const NATIVE_ATTRIBUTE = "data-guidebot-native";
  const BUTTON_ATTRIBUTE = "data-guidebot-select-button";
  const LIST_ATTRIBUTE = "data-guidebot-select-list";
  const FOR_ATTRIBUTE = "data-guidebot-for";
  const OPTION_ATTRIBUTE = "data-guidebot-option";
  const OPTION_INDEX_ATTRIBUTE = "data-guidebot-option-index";
  const OPTION_DISABLED_ATTRIBUTE = "data-guidebot-option-disabled";
  const OPTGROUP_ATTRIBUTE = "data-guidebot-optgroup";
  const ACTIVE_ATTRIBUTE = "data-guidebot-option-active";
  const OVERLAY_SELECTOR = "[" + BUTTON_ATTRIBUTE + "],[" + LIST_ATTRIBUTE + "]";

  // The one selector callers may address an option row with. Every shimmed
  // select on the page owns a row per index, so the bare
  // `[data-guidebot-option-index="N"]` matches once *per select* and trips
  // Playwright's strict mode; the uid scope is what makes it unique:
  //
  //   [data-guidebot-select-list][data-guidebot-for="<uid>"]
  //     [data-guidebot-option-index="<n>"]
  //
  // where `<uid>` is the value of `data-guidebot-shimmed` on the select.

  // Strictly below the cursor's 2147483647 (overlay/cursor.js:18) and its ripple
  // ring/disc, so the synthetic cursor is never buried under the option list.
  // Only observable in popup windows, where cursor and page share a document.
  const BUTTON_Z_INDEX = "2147483639";
  const LIST_Z_INDEX = "2147483640";

  // --- Geometry tuning -------------------------------------------------------
  const LIST_GAP = 2; // px between the control and the list, select2-like
  const VIEWPORT_MARGIN = 8; // px kept clear at the bottom of the frame
  const MIN_LIST_HEIGHT = 120; // hard floor: the list NEVER flips upward
  const FALLBACK_ROW_HEIGHT = 24; // used before a row has been laid out
  const LIST_CHROME_PX = 2; // the list's own top+bottom border

  // --- Appearance ------------------------------------------------------------
  // A plain select2-flavoured dropdown: light border, small radius, blue
  // highlight. Fidelity to the viewer's own browser is explicitly a non-goal;
  // being visible on camera is the whole point.
  const BORDER_COLOR = "#aaaaaa";
  const SURFACE_COLOR = "#ffffff";
  const ACTIVE_BACKGROUND = "#5897fb";
  const ACTIVE_COLOR = "#ffffff";
  const GROUP_COLOR = "#6b7280";
  const SHADOW = "0 2px 8px rgba(15, 23, 42, .18)";
  const FALLBACK_FONT =
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

  const CFG = window.__guidebot_selects_config || {};
  const MODE = CFG.mode === "native" ? "native" : "shim";
  const SETTLE_MS = Math.max(0, Number(CFG.settleMs ?? 1000) || 0);
  const MAX_VISIBLE_OPTIONS = Math.max(1, Number(CFG.maxVisibleOptions ?? 8) || 8);
  // A debounce that only ever re-arms can be starved forever by a page that
  // mutates every frame — and our own overlays do exactly that: `cursor.js`
  // writes `left`/`top` on every frame of a glide, `chrome.js` rewrites its bar
  // every 24 ms while typing a URL. Past this many settle windows the pending
  // classification runs regardless of further mutations.
  const MAX_DEFERRAL_FACTOR = 3;

  // --- Re-injection guard ----------------------------------------------------
  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    typeof previous.refresh === "function"
  ) {
    previous.refresh();
    return;
  }

  let readyResolve = null;
  const ready = new Promise((resolve) => {
    readyResolve = resolve;
  });
  let readyResolved = false;
  function markReady() {
    if (readyResolved) {
      return;
    }
    readyResolved = true;
    readyResolve();
  }

  function installApi(api) {
    api.__guidebotVersion = API_VERSION;
    window[API_KEY] = api;
    // Spec §3 names this global as the readiness barrier; the object property is
    // the contract other agents code against. Keep both pointing at one promise.
    window.__guidebot_selects_ready = api.ready;
  }

  // `mode: native` is the global escape hatch. The API still exists and `ready`
  // still resolves, so compile/render need no branch of their own.
  if (MODE === "native") {
    installApi({
      ready: ready,
      isShimmed: () => false,
      buttonFor: () => null,
      listFor: () => null,
      isOpen: () => false,
      open: () => {},
      close: () => {},
      optionIndexFor: () => -1,
      scrollOptionIntoView: () => {},
      pinNative: () => {},
      refresh: () => {},
      // Nothing is ever classified here, so no pass can be owed.
      settled: () => ready,
    });
    markReady();
    return;
  }

  /** select element -> {uid, select, button, list, open, rect, activeIndex, …} */
  const shims = new Map();
  let uidCounter = 0;
  let rafHandle = null;
  let settleTimer = null;
  // The debounce's ceiling: armed once per chain, never cancelled by a re-arm.
  let deadlineTimer = null;
  let guaranteedTimer = null;

  function css(element, declarations) {
    for (const property of Object.keys(declarations)) {
      element.style.setProperty(property, declarations[property], "important");
    }
  }

  /**
   * The properties a page rule could otherwise use to make the overlay invisible
   * or unreadable — the same defence `cursor.js` mounts (overlay/cursor.js:257).
   *
   * Inline `!important` outranks author `!important`, so every property the
   * overlay declares wins; a property it *omits* is the page's to set, and
   * `div {opacity: .25 !important}` alone is enough to fade the whole widget out
   * of the recording. Anything that must show on camera declares all of these.
   */
  const VISIBILITY_RESET = {
    visibility: "visible",
    opacity: "1",
    transform: "none",
    filter: "none",
    "clip-path": "none",
    // Never `paint`: that would clip the list's own drop shadow.
    contain: "layout style",
  };

  function normalizeLabel(text) {
    return String(text == null ? "" : text)
      .replace(/\s+/g, " ")
      .trim();
  }

  function asElement(node) {
    return node && node.nodeType === 1 ? node : null;
  }

  /**
   * The label of an `<option>`, the way `select_option(label=…)` sees it.
   *
   * Playwright matches on `option.label`, which is the `label` *attribute* when
   * present and the option's text otherwise. Reading `textContent` instead would
   * make `<option label="Krótko">długi tekst</option>` resolve differently in
   * compile than in render — and show text on camera the native control never
   * displays. `textContent` stays as the fallback for the empty-label case.
   */
  function optionLabel(option) {
    const label = option.label;
    return normalizeLabel(label ? label : option.textContent);
  }

  // --- Classification --------------------------------------------------------

  /**
   * True when the page (or a widget library) has already taken this select over.
   *
   * The rule itself lives in `selects/visibility.js` and is published here as
   * `window.__guidebot_select_shape` by the Python controller, which prepends it
   * to this file. It is deliberately not restated inline: the recorder and the
   * compile-time validator ask the same question from Python, in contexts where
   * this widget may not be installed at all, and an answer that drifts between
   * the three is how a select ends up "enhanced" to one of them and "the control
   * the viewer sees" to another.
   *
   * Selects the page deliberately keeps hidden are skipped by the same rule,
   * which is correct — an invisible control must not grow a visible shim.
   */
  function isEnhanced(select) {
    return window.__guidebot_select_shape(select).enhanced;
  }

  /**
   * `multiple` and `size > 1` already render as an in-page listbox with no OS
   * popup, so they record fine as they are and are left untouched.
   *
   * "Not shimmed" therefore means two unrelated things, and a caller must not
   * read it as "the page enhanced this itself": the recorder re-tests
   * `multiple` / `size > 1` on its own and drives such a select by clicking the
   * `<option>` where it already sits.
   */
  function isShimmable(select) {
    if (!select.isConnected) {
      return false;
    }
    // A step that asked for `mode: native` owns this control for the rest of the
    // run; re-shimming it would put the button and list back on top of it right
    // as the recorder is about to set its value directly.
    if (select.hasAttribute(NATIVE_ATTRIBUTE)) {
      return false;
    }
    if (select.multiple || select.size > 1) {
      return false;
    }
    return !isEnhanced(select);
  }

  // --- Shim construction -----------------------------------------------------

  function readFont(select) {
    const computed = window.getComputedStyle(select);
    return {
      "font-family": computed.fontFamily || FALLBACK_FONT,
      "font-size": computed.fontSize || "13px",
      "font-weight": computed.fontWeight || "400",
      "font-style": computed.fontStyle || "normal",
      "letter-spacing": computed.letterSpacing || "normal",
    };
  }

  /**
   * A fully transparent control would leave the shim button see-through, which
   * reads as a rendering glitch on camera; fall back to the list's own surface.
   * Only the alpha channel decides — `rgb(255, 255, 0)` is opaque yellow.
   */
  function opaqueBackground(value) {
    if (!value || value === "transparent") {
      return SURFACE_COLOR;
    }
    const match = /^rgba?\(([^)]*)\)$/.exec(value.trim());
    if (match) {
      const parts = match[1].split(/[,/]/).map((part) => part.trim());
      if (parts.length === 4 && Number.parseFloat(parts[3]) === 0) {
        return SURFACE_COLOR;
      }
    }
    return value;
  }

  function createButton(entry) {
    const select = entry.select;
    const computed = window.getComputedStyle(select);
    const button = document.createElement("div");
    button.setAttribute(BUTTON_ATTRIBUTE, "");
    button.setAttribute(FOR_ATTRIBUTE, entry.uid);
    // Keeps the overlay out of the resolver's candidate set
    // (resolver/page_context.py:186 skips aria-hidden subtrees).
    button.setAttribute("aria-hidden", "true");

    const radius =
      computed.borderRadius && computed.borderRadius !== "0px" ? computed.borderRadius : "4px";
    const padLeft =
      computed.paddingLeft && computed.paddingLeft !== "0px" ? computed.paddingLeft : "8px";
    css(button, {
      ...entry.font,
      ...VISIBILITY_RESET,
      position: "fixed",
      left: "0px",
      top: "0px",
      width: "0px",
      height: "0px",
      // A page `min-height`/`min-width` would otherwise win over the pinned
      // size and inflate the button past the control it stands in for.
      "min-width": "0",
      "max-width": "none",
      "min-height": "0",
      "max-height": "none",
      margin: "0",
      display: "none",
      "align-items": "center",
      "box-sizing": "border-box",
      padding: "0 6px 0 " + padLeft,
      color: computed.color || "#111827",
      "background-color": opaqueBackground(computed.backgroundColor),
      border: "1px solid " + BORDER_COLOR,
      "border-radius": radius,
      "line-height": "normal",
      "text-align": "left",
      "white-space": "nowrap",
      overflow: "hidden",
      // The real <select> stays Playwright's hit target: a click aimed at it
      // must never be intercepted by the shim (Recorder.click, recorder/recorder.py).
      "pointer-events": "none",
      "z-index": BUTTON_Z_INDEX,
    });

    const label = document.createElement("span");
    css(label, {
      ...VISIBILITY_RESET,
      flex: "1 1 auto",
      position: "static",
      margin: "0",
      padding: "0",
      border: "0",
      "font-size": "inherit",
      "line-height": "normal",
      // The width trio matters as much as the height one: a page `span` rule
      // pinning a width would either squeeze the label to nothing or push the
      // caret out of the button.
      width: "auto",
      "min-width": "0",
      "max-width": "none",
      height: "auto",
      "min-height": "0",
      "max-height": "none",
      overflow: "hidden",
      "white-space": "nowrap",
      "text-overflow": "ellipsis",
    });
    button.appendChild(label);

    // A CSS triangle rather than a glyph, so `button.textContent` stays exactly
    // the selected label.
    const caret = document.createElement("span");
    css(caret, {
      ...VISIBILITY_RESET,
      flex: "0 0 auto",
      position: "static",
      display: "block",
      padding: "0",
      margin: "0 0 0 6px",
      // The triangle *is* its borders, so any page-imposed box size deforms it.
      width: "0",
      "min-width": "0",
      "max-width": "none",
      height: "0",
      "min-height": "0",
      "max-height": "none",
      "border-left": "4px solid transparent",
      "border-right": "4px solid transparent",
      "border-top": "5px solid " + (computed.color || "#4b5563"),
      "border-bottom": "0",
    });
    button.appendChild(caret);

    entry.label = label;
    return button;
  }

  function createList(entry) {
    const list = document.createElement("div");
    list.setAttribute(LIST_ATTRIBUTE, "");
    list.setAttribute(FOR_ATTRIBUTE, entry.uid);
    list.setAttribute("aria-hidden", "true");
    list.setAttribute("role", "listbox");
    css(list, {
      ...entry.font,
      ...VISIBILITY_RESET,
      position: "fixed",
      left: "0px",
      top: "0px",
      width: "0px",
      "min-width": "0",
      "max-width": "none",
      // `height` as well as the min/max pair: `max-height` alone leaves
      // `div {height: 120px !important}` free to *shrink* the list below the
      // clamp `layoutList` computed, which silently drops rows out of frame
      // (measured: 120 px and 4 visible options where 218 px and 8 were due).
      height: "auto",
      "min-height": "0",
      display: "none",
      margin: "0",
      padding: "0",
      "box-sizing": "border-box",
      "background-color": SURFACE_COLOR,
      color: "#111827",
      border: "1px solid " + BORDER_COLOR,
      "border-radius": "0 0 4px 4px",
      "box-shadow": SHADOW,
      "max-height": MIN_LIST_HEIGHT + "px",
      "overflow-y": "auto",
      "overflow-x": "hidden",
      "line-height": "normal",
      "z-index": LIST_Z_INDEX,
    });
    list.addEventListener("click", (event) => onListClick(entry, event));
    list.addEventListener("mouseover", (event) => {
      const row = closestRow(event.target);
      if (row && row.getAttribute(FOR_ATTRIBUTE) === entry.uid) {
        setActive(entry, Number(row.getAttribute(OPTION_INDEX_ATTRIBUTE)));
      }
    });
    return list;
  }

  function shim(select) {
    const uid = "gbs-" + ++uidCounter;
    const entry = {
      uid: uid,
      select: select,
      open: false,
      rect: null,
      collapsed: false,
      activeIndex: -1,
      lastIndex: -1,
      font: readFont(select),
    };
    entry.button = createButton(entry);
    entry.list = createList(entry);
    // The select itself is never moved, wrapped or re-parented: its only
    // mutation is this marker attribute. Everything else lives on <body>, so
    // the composed ancestor chain `capture_identity` hashes is unchanged and
    // every frozen target under it keeps matching.
    select.setAttribute(MARKER_ATTRIBUTE, uid);
    document.body.appendChild(entry.button);
    document.body.appendChild(entry.list);
    shims.set(select, entry);
    buildOptions(entry);
    updateLabel(entry);
    pin(entry, true);
    return entry;
  }

  function unshim(select) {
    const entry = shims.get(select);
    if (!entry) {
      return;
    }
    shims.delete(select);
    entry.button.remove();
    entry.list.remove();
    if (select.getAttribute(MARKER_ATTRIBUTE) === entry.uid) {
      select.removeAttribute(MARKER_ATTRIBUTE);
    }
  }

