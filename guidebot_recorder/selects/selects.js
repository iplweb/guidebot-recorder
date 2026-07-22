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

  // --- Option rows -----------------------------------------------------------

  /**
   * Rows carry the full reset too: `layoutList` derives the list's `max-height`
   * from a measured row, so a page `div` rule that resizes rows would silently
   * push options past `max_visible_options`.
   */
  function rowStyle(row, disabled) {
    css(row, {
      ...VISIBILITY_RESET,
      display: "block",
      position: "static",
      float: "none",
      padding: "6px 8px",
      margin: "0",
      border: "0",
      width: "auto",
      "min-width": "0",
      "max-width": "none",
      height: "auto",
      "min-height": "0",
      "max-height": "none",
      "font-size": "inherit",
      "line-height": "normal",
      "box-sizing": "border-box",
      "white-space": "nowrap",
      overflow: "hidden",
      "text-overflow": "ellipsis",
      cursor: disabled ? "default" : "pointer",
      opacity: disabled ? "0.45" : "1",
      "background-color": "transparent",
      color: "inherit",
    });
  }

  function createRow(entry, option) {
    const row = document.createElement("div");
    row.setAttribute(OPTION_ATTRIBUTE, "");
    // Options are addressed by index, never by label text: labels repeat across
    // optgroups and may contain quotes or backslashes that would need escaping.
    row.setAttribute(OPTION_INDEX_ATTRIBUTE, String(option.index));
    row.setAttribute(FOR_ATTRIBUTE, entry.uid);
    row.setAttribute("role", "option");
    if (option.disabled) {
      row.setAttribute(OPTION_DISABLED_ATTRIBUTE, "");
    }
    row.textContent = optionLabel(option);
    rowStyle(row, option.disabled);
    return row;
  }

  function createGroupHeading(group) {
    const heading = document.createElement("div");
    heading.setAttribute(OPTGROUP_ATTRIBUTE, "");
    heading.setAttribute("role", "presentation");
    heading.textContent = normalizeLabel(group.getAttribute("label") || "");
    css(heading, {
      ...VISIBILITY_RESET,
      display: "block",
      position: "static",
      float: "none",
      padding: "6px 8px 2px 8px",
      margin: "0",
      border: "0",
      width: "auto",
      "min-width": "0",
      "max-width": "none",
      height: "auto",
      "min-height": "0",
      "max-height": "none",
      "font-size": "inherit",
      "line-height": "normal",
      "box-sizing": "border-box",
      "font-weight": "700",
      color: GROUP_COLOR,
      cursor: "default",
      "white-space": "nowrap",
      overflow: "hidden",
      "text-overflow": "ellipsis",
    });
    return heading;
  }

  // Spelled as an escape on purpose. This used to be the literal control
  // character, which is invisible in an editor, in a diff and in review — it
  // reads exactly like `join("")`, the separator-less join under which
  // `["a", "b"]` and `["ao:b"]` fingerprint identically.
  const SIGNATURE_SEPARATOR = "\u0001";

  /**
   * A cheap fingerprint of everything `buildOptions` renders.
   *
   * Rebuilding unconditionally on every classification pass would reset the
   * highlight and the scroll position of an open list; rebuilding only when this
   * changes keeps the rows fresh without the churn.
   *
   * The parts are joined with a separator no label can contain, so two distinct
   * option sets can never share a fingerprint and leave the rows stale.
   */
  function optionsSignature(select) {
    const parts = [];
    for (const child of Array.from(select.children)) {
      const tag = child.tagName.toLowerCase();
      if (tag === "option") {
        parts.push("o:" + optionLabel(child) + (child.disabled ? ":d" : ""));
      } else if (tag === "optgroup") {
        parts.push("g:" + normalizeLabel(child.getAttribute("label") || ""));
        for (const option of Array.from(child.children)) {
          if (option.tagName.toLowerCase() === "option") {
            parts.push("go:" + optionLabel(option) + (option.disabled ? ":d" : ""));
          }
        }
      }
    }
    return parts.join(SIGNATURE_SEPARATOR);
  }

  /** Rebuild the rows if — and only if — the option set actually changed. */
  function syncOptions(entry) {
    const signature = optionsSignature(entry.select);
    if (signature === entry.signature) {
      return;
    }
    buildOptions(entry);
    if (entry.open) {
      // The highlight belongs to the *open list*, not to the select: the viewer
      // may have arrowed it far away from `selectedIndex`, and re-applying the
      // selection would throw that away — measured, a highlight on row 12 jumped
      // back to row 0 the moment the page appended an option.
      setActive(entry, entry.activeIndex >= 0 ? entry.activeIndex : entry.select.selectedIndex);
    }
  }

  function buildOptions(entry) {
    const list = entry.list;
    entry.signature = optionsSignature(entry.select);
    list.textContent = "";
    for (const child of Array.from(entry.select.children)) {
      const tag = child.tagName.toLowerCase();
      if (tag === "option") {
        list.appendChild(createRow(entry, child));
      } else if (tag === "optgroup") {
        list.appendChild(createGroupHeading(child));
        for (const option of Array.from(child.children)) {
          if (option.tagName.toLowerCase() === "option") {
            const row = createRow(entry, option);
            css(row, { "padding-left": "20px" });
            list.appendChild(row);
          }
        }
      }
    }
  }

  function rowFor(entry, index) {
    for (const row of entry.list.children) {
      if (row.getAttribute(OPTION_INDEX_ATTRIBUTE) === String(index)) {
        return row;
      }
    }
    return null;
  }

  function setActive(entry, index) {
    for (const row of entry.list.children) {
      if (!row.hasAttribute(OPTION_INDEX_ATTRIBUTE)) {
        continue;
      }
      const isActive = row.getAttribute(OPTION_INDEX_ATTRIBUTE) === String(index);
      const disabled = row.hasAttribute(OPTION_DISABLED_ATTRIBUTE);
      if (isActive && !disabled) {
        row.setAttribute(ACTIVE_ATTRIBUTE, "");
        css(row, { "background-color": ACTIVE_BACKGROUND, color: ACTIVE_COLOR });
      } else {
        row.removeAttribute(ACTIVE_ATTRIBUTE);
        css(row, { "background-color": "transparent", color: "inherit" });
      }
    }
    entry.activeIndex = index;
  }

  function updateLabel(entry) {
    const select = entry.select;
    const option = select.options[select.selectedIndex];
    entry.label.textContent = option ? optionLabel(option) : "";
    entry.lastIndex = select.selectedIndex;
  }

  // --- Geometry --------------------------------------------------------------

  /**
   * Re-pin the overlay onto the select's current viewport rect.
   *
   * `position: fixed` at <body> level means no `overflow: hidden` ancestor and
   * no ancestor stacking context can clip or bury the list — the same reason
   * select2 appends its own list to <body>.
   */
  function pin(entry, force) {
    const select = entry.select;
    const rect = select.getBoundingClientRect();
    // A detached or collapsed select must lose its dropdown *this frame*: the
    // debounced `classify()` is what eventually unshims it, and a whole
    // `settle_ms` of a ghost list pinned to a zero rect lands on camera.
    const collapsed = !select.isConnected || rect.width < 1 || rect.height < 1;
    if (collapsed && entry.open) {
      closeList(select);
    }
    const last = entry.rect;
    if (
      !force &&
      last &&
      collapsed === entry.collapsed &&
      last.left === rect.left &&
      last.top === rect.top &&
      last.width === rect.width &&
      last.height === rect.height
    ) {
      return;
    }
    entry.rect = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    entry.collapsed = collapsed;
    css(entry.button, {
      display: collapsed ? "none" : "flex",
      left: rect.left + "px",
      top: rect.top + "px",
      width: rect.width + "px",
      height: rect.height + "px",
    });
    if (entry.open) {
      layoutList(entry, rect);
    }
  }

  /**
   * Lay the list out **downward, always** (a hard requirement of the design).
   *
   * When there is not enough room below, `max-height` is clamped to what is
   * available — floored at MIN_LIST_HEIGHT — and the list scrolls internally.
   * It never flips above the control.
   */
  /**
   * Height that shows the first MAX_VISIBLE_OPTIONS *options*.
   *
   * Optgroup headings are counted but do not consume the budget: counting only
   * option rows (as this once did) lets every heading push a real option out of
   * sight, so a grouped select shows fewer choices than configured.
   */
  function preferredHeight(list) {
    let shown = 0;
    let total = 0;
    for (const row of list.children) {
      const isOption = row.hasAttribute(OPTION_INDEX_ATTRIBUTE);
      if (isOption && shown >= MAX_VISIBLE_OPTIONS) {
        break;
      }
      const measured = row.getBoundingClientRect().height;
      total += measured > 0 ? measured : FALLBACK_ROW_HEIGHT;
      if (isOption) {
        shown += 1;
      }
    }
    return Math.ceil(total) + LIST_CHROME_PX;
  }

  function layoutList(entry, rect) {
    const list = entry.list;
    const top = rect.top + rect.height + LIST_GAP;
    const available = Math.max(0, window.innerHeight - top - VIEWPORT_MARGIN);
    const maxHeight = Math.max(MIN_LIST_HEIGHT, Math.min(preferredHeight(list), available));
    css(list, {
      left: rect.left + "px",
      top: top + "px",
      width: rect.width + "px",
      "max-height": maxHeight + "px",
    });
  }

  function pinAll(force) {
    for (const entry of shims.values()) {
      pin(entry, force);
      if (entry.select.selectedIndex !== entry.lastIndex) {
        updateLabel(entry);
      }
    }
  }

  function tick() {
    rafHandle = null;
    if (shims.size === 0) {
      return;
    }
    pinAll(false);
    scheduleFrame();
  }

  /** The rAF loop runs only while at least one shim exists. */
  function scheduleFrame() {
    if (rafHandle === null && shims.size > 0) {
      rafHandle = window.requestAnimationFrame(tick);
    }
  }

  // --- Opening, closing, choosing --------------------------------------------

  function openList(select) {
    const entry = shims.get(select);
    if (!entry) {
      return;
    }
    for (const other of shims.values()) {
      if (other !== entry && other.open) {
        closeList(other.select);
      }
    }
    // The page may have replaced the option set since the last pass.
    syncOptions(entry);
    updateLabel(entry);
    entry.open = true;
    css(entry.list, { display: "block" });
    layoutList(entry, select.getBoundingClientRect());
    setActive(entry, select.selectedIndex);
    scrollOptionIntoView(select, select.selectedIndex);
    try {
      select.focus({ preventScroll: true });
    } catch (_error) {
      /* a page focus handler threw; the list is open either way */
    }
  }

  function closeList(select) {
    const entry = shims.get(select);
    if (!entry || !entry.open) {
      return;
    }
    entry.open = false;
    css(entry.list, { display: "none" });
  }

  function closeAll() {
    for (const entry of shims.values()) {
      closeList(entry.select);
    }
  }

  /**
   * Commit a choice exactly the way a native select does — including staying
   * silent when the value does not actually change. Dispatching unconditionally
   * would make a page with an expensive `change` handler behave differently
   * under render than under compile's `select_option` (spec §4).
   */
  function choose(entry, index) {
    const select = entry.select;
    if (!Number.isInteger(index) || index < 0 || index >= select.options.length) {
      return;
    }
    if (select.options[index].disabled) {
      return;
    }
    closeList(select);
    if (select.selectedIndex === index) {
      return;
    }
    select.selectedIndex = index;
    updateLabel(entry);
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function closestRow(target) {
    const element = asElement(target);
    return element ? element.closest("[" + OPTION_INDEX_ATTRIBUTE + "]") : null;
  }

  function onListClick(entry, event) {
    const row = closestRow(event.target);
    if (!row || row.hasAttribute(OPTION_DISABLED_ATTRIBUTE)) {
      return;
    }
    choose(entry, Number(row.getAttribute(OPTION_INDEX_ATTRIBUTE)));
  }

  // --- Input handling --------------------------------------------------------

  /**
   * The elements an event actually travelled through, shadow boundaries included.
   *
   * `event.target` is retargeted to the shadow *host* for anything inside an
   * open shadow root, so `closest()` on it can never find the select the user
   * pressed. `composedPath()` is what sees through the boundary.
   */
  function eventPath(event) {
    const path = typeof event.composedPath === "function" ? event.composedPath() : null;
    return path && path.length > 0 ? path : [event.target];
  }

  function shimmedSelectFrom(event) {
    for (const node of eventPath(event)) {
      const element = asElement(node);
      if (element && shims.has(element)) {
        return element;
      }
    }
    return null;
  }

  function overlayFrom(event) {
    for (const node of eventPath(event)) {
      const element = asElement(node);
      if (element && element.closest && element.closest(OVERLAY_SELECTOR)) {
        return element;
      }
    }
    return null;
  }

  function onMouseDown(event) {
    if (overlayFrom(event)) {
      // Keep focus (and therefore the open list) while the click lands.
      event.preventDefault();
      return;
    }
    if (event.button !== 0) {
      return;
    }
    const select = shimmedSelectFrom(event);
    if (!select) {
      closeAll();
      return;
    }
    // preventDefault() on mousedown is what suppresses Chromium's native,
    // un-recordable OS popup. The DOM list takes its place.
    event.preventDefault();
    if (shims.get(select).open) {
      closeList(select);
    } else {
      openList(select);
    }
  }

  function nextEnabledIndex(select, from, step) {
    const count = select.options.length;
    for (let index = from + step; index >= 0 && index < count; index += step) {
      if (!select.options[index].disabled) {
        return index;
      }
    }
    return -1;
  }

  function onKeyDown(event) {
    const select = shimmedSelectFrom(event);
    if (!select) {
      return;
    }
    const entry = shims.get(select);
    const key = event.key;
    if (!entry.open) {
      if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
        event.preventDefault();
        openList(select);
      }
      return;
    }
    if (key === "ArrowDown" || key === "ArrowUp") {
      event.preventDefault();
      const next = nextEnabledIndex(select, entry.activeIndex, key === "ArrowDown" ? 1 : -1);
      if (next >= 0) {
        setActive(entry, next);
        scrollOptionIntoView(select, next);
      }
    } else if (key === "Enter" || key === " ") {
      event.preventDefault();
      choose(entry, entry.activeIndex);
    } else if (key === "Escape") {
      event.preventDefault();
      closeList(select);
    }
  }

  // --- Classification pass and observer --------------------------------------

  /**
   * Every `<select>` in the document, descending into **open** shadow roots.
   *
   * Closed roots are unreachable by design and stay native. Each root found is
   * also handed to the observer, so a select added to it later is classified
   * like any other. A root attached *after* this sweep produces no mutation
   * record of its own (`attachShadow` is invisible to a MutationObserver); it is
   * picked up by the next pass the host's own mutations trigger.
   *
   * The `"*"` sweep is the price of that coverage, and it stays: a shadow root
   * is reachable only through its host's `shadowRoot` property, which no
   * selector can express, so the native `querySelectorAll("select")` this
   * replaced cannot see into one. Passes are debounced by `settle_ms` and
   * happen only on mutation, so the walk is not per frame. `localName` rather
   * than `tagName.toLowerCase()` keeps it from allocating a string per element.
   */
  function collectSelects() {
    const found = [];
    const pending = [document];
    while (pending.length > 0) {
      const root = pending.pop();
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) {
          pending.push(element.shadowRoot);
          observeRoot(element.shadowRoot);
        }
        if (element.localName === "select") {
          found.push(element);
        }
      }
    }
    return found;
  }

  // --- The pending-pass barrier ----------------------------------------------
  //
  // `ready` answers "has the *first* pass run?" — once, and never again. That is
  // the wrong question for a step that drives a select the page has only just
  // added: the observer has already armed a pass, the select is not classified
  // yet, and `ready` settled seconds ago at page load. A caller that trusts it
  // finds a bare `<select>` with no DOM list to unfurl, and the step fails with
  // "the shim did not cover it" — for a select the shim was about to cover.
  //
  // `settled()` answers the question that actually gates such a step: "is a
  // classification pass still owed?".

  /** Resolvers waiting for the pass that was owed when they asked. */
  let passWaiters = [];

  function notifyPassComplete() {
    if (passWaiters.length === 0) {
      return;
    }
    const waiting = passWaiters;
    passWaiters = [];
    for (const resolve of waiting) {
      resolve();
    }
  }

  function classificationPending() {
    return settleTimer !== null || deadlineTimer !== null || guaranteedTimer !== null;
  }

  /**
   * A promise for the classification pass that is owed *right now*.
   *
   * Snapshot semantics, deliberately not "resolve once the page is quiescent":
   * a document that keeps mutating is never quiescent — our own `cursor.js`
   * rewrites `left`/`top` every frame of a glide — so a barrier waiting for that
   * would hang every step instead of unblocking the one that needed it. Waiting
   * for the pass owed at the moment of asking is bounded by the debounce's own
   * ceiling (`MAX_DEFERRAL_FACTOR` settle windows) no matter what the page does
   * afterwards, which is the same bound `Selects.ready_timeout` already derives.
   *
   * With nothing owed this is `ready`, so the first-pass barrier is still taken
   * on a page that has not classified anything yet.
   */
  function settled() {
    if (!classificationPending()) {
      return ready;
    }
    return new Promise((resolve) => passWaiters.push(resolve));
  }

  function classify() {
    if (!document.body) {
      // Nothing to append to yet; retry on the uncancellable timer rather than
      // resolving `ready` early or handing the retry to the starvable debounce.
      scheduleGuaranteedPass();
      return;
    }
    // Everything below runs against page-controlled DOM, so a page getter, a
    // patched prototype or a hostile `getComputedStyle` can throw anywhere in
    // it. The `finally` is what keeps such a throw cosmetic: without it the pass
    // takes `markReady()` down with it, and once the guaranteed timer has fired
    // there is nothing left to resolve `ready` — compile and render then block
    // on it until their own timeout.
    try {
      for (const select of Array.from(shims.keys())) {
        if (!isShimmable(select)) {
          unshim(select);
          continue;
        }
        // An SPA that replaces <body> takes our overlays with it while the
        // select survives; re-attach rather than losing the shim.
        const entry = shims.get(select);
        if (!entry.button.isConnected) {
          document.body.appendChild(entry.button);
        }
        if (!entry.list.isConnected) {
          document.body.appendChild(entry.list);
        }
        // Rows read stale text until the next `open()` otherwise; the page may
        // have swapped the whole option set in the meantime.
        syncOptions(entry);
        updateLabel(entry);
      }
      for (const select of collectSelects()) {
        if (shims.has(select) || !isShimmable(select)) {
          continue;
        }
        shim(select);
      }
    } finally {
      scheduleFrame();
      markReady();
      // A pass has now completed, so the fallback timer has served its purpose;
      // from here the capped debounce is enough to keep classification alive.
      if (guaranteedTimer !== null) {
        window.clearTimeout(guaranteedTimer);
        guaranteedTimer = null;
      }
      // Inside the `finally` for the same reason `markReady` is: a pass that
      // throws still has to release the callers blocked on it, or one page
      // getter takes compile and render down with it.
      notifyPassComplete();
    }
  }

  /** Run a pending pass now and retire both timers that could still start one. */
  function runPendingClassify() {
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
      settleTimer = null;
    }
    if (deadlineTimer !== null) {
      window.clearTimeout(deadlineTimer);
      deadlineTimer = null;
    }
    classify();
  }

  /**
   * Debounced classification, with a hard ceiling on how long it may be deferred.
   *
   * Every re-arm restarts the settle window, so a page that mutates constantly
   * would postpone the pass forever. The ceiling that prevents that is a *second,
   * independent* timer, armed once when the chain starts and never cancelled by
   * a re-arm — exactly what `scheduleGuaranteedPass` does for the first pass.
   *
   * Implementing the ceiling by shortening the re-armed debounce instead (clear +
   * set, with the wait clamped to the remaining budget) does not hold: a page
   * running `setTimeout(fn, 0)` re-queues its own work ahead of every freshly set
   * timer, so the ceiling never comes due. Measured that way, a select added
   * during such a storm was still unshimmed after 5 s.
   */
  function scheduleClassify(delay) {
    const requested = delay === undefined ? SETTLE_MS : delay;
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
    }
    settleTimer = window.setTimeout(() => {
      settleTimer = null;
      runPendingClassify();
    }, requested);
    if (deadlineTimer === null) {
      deadlineTimer = window.setTimeout(() => {
        deadlineTimer = null;
        runPendingClassify();
      }, Math.max(requested, SETTLE_MS * MAX_DEFERRAL_FACTOR));
    }
  }

  /**
   * The first pass, on a timer nothing cancels — not the debounce above, not
   * `refresh()`. `ready` is the barrier compile and render block on, so it must
   * settle even when the page never stops mutating.
   *
   * It fires at the same ceiling the debounce is capped at, so on a quiet page
   * the debounce still runs the first pass one settle window in — the head start
   * a widget library needs — and only a churning page falls back to this.
   */
  function scheduleGuaranteedPass() {
    if (guaranteedTimer !== null) {
      return;
    }
    guaranteedTimer = window.setTimeout(() => {
      guaranteedTimer = null;
      classify();
    }, SETTLE_MS * MAX_DEFERRAL_FACTOR);
  }

  const OWN_ATTRIBUTES = [
    BUTTON_ATTRIBUTE,
    LIST_ATTRIBUTE,
    OPTION_ATTRIBUTE,
    OPTGROUP_ATTRIBUTE,
    FOR_ATTRIBUTE,
  ];

  function isOurNode(node) {
    const element = asElement(node);
    if (!element) {
      return false;
    }
    // Option rows are checked by their own attributes as well as by ancestry:
    // a *removed* row is already detached, so `closest` no longer finds the list.
    for (const name of OWN_ATTRIBUTES) {
      if (element.hasAttribute(name)) {
        return true;
      }
    }
    return element.closest(OVERLAY_SELECTOR) !== null;
  }

  /**
   * Ignore the mutations the shim itself causes. Without this the observer would
   * re-arm the settle timer on every re-pin and classify forever.
   */
  function isRelevant(records) {
    for (const record of records) {
      // A MutationObserver cannot unobserve a single node, so a shadow root the
      // page has thrown away keeps delivering records forever. Nothing inside it
      // can reach the document any more, so reacting would only re-arm the
      // settle debounce on behalf of a dead subtree — measured, that pushed a
      // live select's shim from 0.20 s out to the 0.42 s deferral ceiling.
      if (!record.target.isConnected) {
        continue;
      }
      if (record.type === "attributes") {
        if (isOurNode(record.target)) {
          continue;
        }
        return true;
      }
      if (isOurNode(record.target)) {
        continue;
      }
      const touched = [...record.addedNodes, ...record.removedNodes].filter(
        (node) => node.nodeType === 1,
      );
      if (touched.length === 0) {
        continue;
      }
      if (touched.every(isOurNode)) {
        continue;
      }
      return true;
    }
    return false;
  }

  // The same settle_ms debounce as the first pass, so the shim never wins a race
  // against the page's own widget initialisation.
  const observer = new MutationObserver((records) => {
    if (isRelevant(records)) {
      scheduleClassify();
    }
  });

  const OBSERVER_OPTIONS = {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "style", "hidden", "multiple", "size", "disabled"],
  };

  /**
   * Observe a root once. Shadow roots need their own registration.
   *
   * Weak on purpose: this bookkeeping only answers "already registered?", and a
   * strong `Set` would pin every shadow root the page ever attached for the
   * lifetime of the document. The observer's own node list holds weak references
   * (DOM standard), so once this set lets go, a detached root is collectable.
   */
  const observedRoots = new WeakSet();
  function observeRoot(root) {
    if (observedRoots.has(root)) {
      return;
    }
    observedRoots.add(root);
    observer.observe(root, OBSERVER_OPTIONS);
  }

  /**
   * Watch the `document`, not `documentElement`.
   *
   * A MutationObserver holds the *node* it was given, and `document.open()` —
   * which is what `document.write()` and Playwright's `setContent` run on —
   * replaces `documentElement` outright. Bound to that element, the observer is
   * left watching a detached tree and never reports another mutation: the shim
   * stops classifying for the life of the document, so every select the page
   * adds from then on stays bare. `document` itself is never replaced, and
   * `subtree: true` reaches everything under whichever root is current.
   */
  function startObserving() {
    observeRoot(document);
  }

  // --- Public API ------------------------------------------------------------

  function resolveSelect(element) {
    const node = asElement(element);
    if (!node) {
      return null;
    }
    if (node.tagName.toLowerCase() === "select") {
      return node;
    }
    const uid = node.getAttribute(FOR_ATTRIBUTE);
    if (!uid) {
      return null;
    }
    for (const [select, entry] of shims) {
      if (entry.uid === uid) {
        return select;
      }
    }
    return null;
  }

  function entryFor(element) {
    const select = resolveSelect(element);
    return select ? shims.get(select) || null : null;
  }

  /**
   * Index into `select.options` of the option whose label matches `label`.
   *
   * Whitespace is collapsed and trimmed on both sides; the comparison is then
   * exact. Returns -1 when absent.
   *
   * Case-insensitive matching used to be the fallback here and nowhere else:
   * compile drives through Playwright's `select_option(label=…)` and the
   * listbox path through `_OPTION_INDEX_JS`, both exact. A scenario whose
   * option label differed only in case therefore resolved one way on a shimmed
   * select and failed outright on the other two shapes — the same scenario
   * behaving differently depending on the control's shape is exactly the drift
   * this branch exists to remove.
   */
  function optionIndexFor(element, label) {
    const select = resolveSelect(element);
    if (!select) {
      return -1;
    }
    const wanted = normalizeLabel(label);
    const options = select.options;
    for (let index = 0; index < options.length; index += 1) {
      if (optionLabel(options[index]) === wanted) {
        return index;
      }
    }
    return -1;
  }

  /** Scroll the list so option ``index`` sits centred in its scroll box. */
  function scrollOptionIntoView(element, index) {
    const entry = entryFor(element);
    if (!entry) {
      return;
    }
    const row = rowFor(entry, index);
    if (!row) {
      return;
    }
    const list = entry.list;
    const target = row.offsetTop - (list.clientHeight - row.offsetHeight) / 2;
    list.scrollTop = Math.max(0, Math.min(target, list.scrollHeight - list.clientHeight));
  }

  installApi({
    ready: ready,
    isShimmed: (element) => {
      const select = resolveSelect(element);
      return !!select && shims.has(select);
    },
    buttonFor: (element) => {
      const entry = entryFor(element);
      return entry ? entry.button : null;
    },
    listFor: (element) => {
      const entry = entryFor(element);
      return entry ? entry.list : null;
    },
    isOpen: (element) => {
      const entry = entryFor(element);
      return !!entry && entry.open;
    },
    open: (element) => {
      const select = resolveSelect(element);
      if (select) {
        openList(select);
      }
    },
    close: (element) => {
      const select = resolveSelect(element);
      if (select) {
        closeList(select);
      }
    },
    optionIndexFor: optionIndexFor,
    scrollOptionIntoView: scrollOptionIntoView,
    /**
     * Hand one select back to the browser — the per-step `mode: native` hatch.
     *
     * Under a global `shim` the select the step wants native is already shimmed,
     * so opting out has to undo the shim *and* stay undone: the marker is what
     * `isShimmable` reads, so no later classification pass reshims the control
     * out from under the recorder before it sets the value directly.
     *
     * Deliberately one-way for the life of the document. A step is the only
     * caller, its choice is per-scenario and static, and a select that flipped
     * back to shimmed mid-run would be exactly the race this removes.
     */
    pinNative: (element) => {
      const select = resolveSelect(element);
      if (!select) {
        return;
      }
      select.setAttribute(NATIVE_ATTRIBUTE, "");
      unshim(select);
    },
    /**
     * Re-arm the settle debounce — deliberately *not* an immediate pass.
     *
     * The re-injection guard calls this, and injection happens at t≈0 of a
     * document (`add_init_script` plus an explicit `evaluate`, the idiom
     * `Overlay.install` sets). Classifying there would beat the page's own
     * widget initialisation, which is the race `settle_ms` exists to prevent.
     */
    refresh: () => {
      scheduleClassify();
    },
    settled: settled,
  });

  document.addEventListener("mousedown", onMouseDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  // Capture phase, so a select inside an internally scrolling container counts
  // (`scroll` does not bubble). Unforced: a scroll that moved no shim must cost
  // no style writes — the rect comparison in `pin` is the whole point, and the
  // rAF loop would re-pin the same frame anyway.
  window.addEventListener("scroll", () => pinAll(false), true);
  // Forced, unlike scroll: `layoutList` reads `window.innerHeight`, so the list
  // needs a new clamp even when the control itself has not moved.
  window.addEventListener("resize", () => pinAll(true));

  function start() {
    startObserving();
    // Both: the debounce gives the page its settle window, the fallback timer
    // guarantees the pass happens even if that window never closes.
    scheduleClassify();
    scheduleGuaranteedPass();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
  if (document.readyState !== "complete") {
    // "settle_ms after load": re-arm the debounce so late enhancement wins.
    window.addEventListener("load", () => scheduleClassify(), { once: true });
  }
})();
