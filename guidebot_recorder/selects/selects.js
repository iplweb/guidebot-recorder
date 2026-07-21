(() => {
  "use strict";

  // --- Role gating -----------------------------------------------------------
  // Read the REAL `window.top` here, before chrome.js can shadow it. The
  // registration order is a documented contract (recorder/render.py:1989-1995):
  // this script is installed alongside cursor.js/slide.js, ahead of chrome.js.
  //
  // Unlike those overlays, the shim belongs to the *site*, not to the top
  // window: it must run inside the framed site (isTop === false) and inside
  // top-level popup site documents, but never in the shell, which holds no page
  // content — only the address bar and the site iframe.
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
  const BUTTON_ATTRIBUTE = "data-guidebot-select-button";
  const LIST_ATTRIBUTE = "data-guidebot-select-list";
  const FOR_ATTRIBUTE = "data-guidebot-for";
  const OPTION_ATTRIBUTE = "data-guidebot-option";
  const OPTION_INDEX_ATTRIBUTE = "data-guidebot-option-index";
  const OPTION_DISABLED_ATTRIBUTE = "data-guidebot-option-disabled";
  const OPTGROUP_ATTRIBUTE = "data-guidebot-optgroup";
  const ACTIVE_ATTRIBUTE = "data-guidebot-option-active";
  const OVERLAY_SELECTOR = "[" + BUTTON_ATTRIBUTE + "],[" + LIST_ATTRIBUTE + "]";

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

  // Belt-and-braces only: the geometric test below is the primary signal,
  // because it is library-agnostic (select2 clips the original to 1x1 px,
  // Tom Select uses display:none, Chosen hides it too).
  const MARKER_CLASSES = ["select2-hidden-accessible", "tomselected", "chosen-select"];

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
      refresh: () => {},
    });
    markReady();
    return;
  }

  /** select element -> {uid, select, button, list, open, rect, activeIndex, …} */
  const shims = new Map();
  let uidCounter = 0;
  let rafHandle = null;
  let settleTimer = null;

  function css(element, declarations) {
    for (const property of Object.keys(declarations)) {
      element.style.setProperty(property, declarations[property], "important");
    }
  }

  function normalizeLabel(text) {
    return String(text == null ? "" : text)
      .replace(/\s+/g, " ")
      .trim();
  }

  function asElement(node) {
    return node && node.nodeType === 1 ? node : null;
  }

  // --- Classification --------------------------------------------------------

  /**
   * True when the page (or a widget library) has already taken this select over.
   *
   * The geometric test is the primary signal on purpose: every library that
   * enhances a select keeps the original and merely hides it. Selects the page
   * deliberately keeps hidden are skipped by the same rule, which is correct —
   * an invisible control must not grow a visible shim.
   */
  function isEnhanced(select) {
    const computed = window.getComputedStyle(select);
    if (computed.display === "none" || computed.visibility === "hidden") {
      return true;
    }
    const rect = select.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) {
      return true;
    }
    for (const name of MARKER_CLASSES) {
      if (select.classList.contains(name)) {
        return true;
      }
    }
    return false;
  }

  /**
   * `multiple` and `size > 1` already render as an in-page listbox with no OS
   * popup, so they record fine as they are and are left untouched.
   */
  function isShimmable(select) {
    if (!select.isConnected) {
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
      position: "fixed",
      left: "0px",
      top: "0px",
      width: "0px",
      height: "0px",
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
      // must never be intercepted by the shim (Recorder.click, recorder.py:94).
      "pointer-events": "none",
      "z-index": BUTTON_Z_INDEX,
    });

    const label = document.createElement("span");
    css(label, {
      flex: "1 1 auto",
      overflow: "hidden",
      "white-space": "nowrap",
      "text-overflow": "ellipsis",
    });
    button.appendChild(label);

    // A CSS triangle rather than a glyph, so `button.textContent` stays exactly
    // the selected label.
    const caret = document.createElement("span");
    css(caret, {
      flex: "0 0 auto",
      "margin-left": "6px",
      width: "0",
      height: "0",
      "border-left": "4px solid transparent",
      "border-right": "4px solid transparent",
      "border-top": "5px solid " + (computed.color || "#4b5563"),
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
      position: "fixed",
      left: "0px",
      top: "0px",
      width: "0px",
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

  function rowStyle(row, disabled) {
    css(row, {
      display: "block",
      padding: "6px 8px",
      margin: "0",
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
    row.textContent = normalizeLabel(option.textContent);
    rowStyle(row, option.disabled);
    return row;
  }

  function createGroupHeading(group) {
    const heading = document.createElement("div");
    heading.setAttribute(OPTGROUP_ATTRIBUTE, "");
    heading.setAttribute("role", "presentation");
    heading.textContent = normalizeLabel(group.getAttribute("label") || "");
    css(heading, {
      display: "block",
      padding: "6px 8px 2px 8px",
      margin: "0",
      "font-weight": "700",
      color: GROUP_COLOR,
      cursor: "default",
      "white-space": "nowrap",
      overflow: "hidden",
      "text-overflow": "ellipsis",
    });
    return heading;
  }

  function buildOptions(entry) {
    const list = entry.list;
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
    entry.label.textContent = option ? normalizeLabel(option.textContent) : "";
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
    const rect = entry.select.getBoundingClientRect();
    const last = entry.rect;
    if (
      !force &&
      last &&
      last.left === rect.left &&
      last.top === rect.top &&
      last.width === rect.width &&
      last.height === rect.height
    ) {
      return;
    }
    entry.rect = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    const collapsed = rect.width < 1 || rect.height < 1;
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
  function layoutList(entry, rect) {
    const list = entry.list;
    const top = rect.top + rect.height + LIST_GAP;
    const available = Math.max(0, window.innerHeight - top - VIEWPORT_MARGIN);
    let rowHeight = FALLBACK_ROW_HEIGHT;
    for (const row of list.children) {
      if (row.hasAttribute(OPTION_INDEX_ATTRIBUTE)) {
        const measured = row.getBoundingClientRect().height;
        if (measured > 0) {
          rowHeight = measured;
        }
        break;
      }
    }
    const preferred = Math.ceil(rowHeight * MAX_VISIBLE_OPTIONS) + LIST_CHROME_PX;
    const maxHeight = Math.max(MIN_LIST_HEIGHT, Math.min(preferred, available));
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
    // Rebuild every time: the page may have replaced the option set since the
    // shim was attached.
    buildOptions(entry);
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

  function shimmedSelectFrom(target) {
    const element = asElement(target);
    if (!element) {
      return null;
    }
    const select = element.closest("select[" + MARKER_ATTRIBUTE + "]");
    return select && shims.has(select) ? select : null;
  }

  function onMouseDown(event) {
    const element = asElement(event.target);
    if (element && element.closest(OVERLAY_SELECTOR)) {
      // Keep focus (and therefore the open list) while the click lands.
      event.preventDefault();
      return;
    }
    if (event.button !== 0) {
      return;
    }
    const select = shimmedSelectFrom(event.target);
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
    const select = shimmedSelectFrom(event.target);
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

  function classify() {
    if (!document.body) {
      // Nothing to append to yet; try again rather than resolving `ready` early.
      scheduleClassify();
      return;
    }
    for (const select of Array.from(shims.keys())) {
      if (!isShimmable(select)) {
        unshim(select);
        continue;
      }
      // An SPA that replaces <body> takes our overlays with it while the select
      // survives; re-attach rather than losing the shim.
      const entry = shims.get(select);
      if (!entry.button.isConnected) {
        document.body.appendChild(entry.button);
      }
      if (!entry.list.isConnected) {
        document.body.appendChild(entry.list);
      }
    }
    for (const select of Array.from(document.querySelectorAll("select"))) {
      if (shims.has(select) || !isShimmable(select)) {
        continue;
      }
      shim(select);
    }
    scheduleFrame();
    markReady();
  }

  function scheduleClassify(delay) {
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
    }
    settleTimer = window.setTimeout(
      () => {
        settleTimer = null;
        classify();
      },
      delay === undefined ? SETTLE_MS : delay,
    );
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
      if (record.type === "attributes") {
        if (record.attributeName === MARKER_ATTRIBUTE) {
          continue;
        }
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

  function startObserving() {
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class", "style", "hidden", "multiple", "size", "disabled"],
    });
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
   * Whitespace is collapsed and trimmed on both sides; an exact match wins and a
   * case-insensitive match is the fallback. Returns -1 when absent.
   */
  function optionIndexFor(element, label) {
    const select = resolveSelect(element);
    if (!select) {
      return -1;
    }
    const wanted = normalizeLabel(label);
    const options = select.options;
    for (let index = 0; index < options.length; index += 1) {
      if (normalizeLabel(options[index].textContent) === wanted) {
        return index;
      }
    }
    const lowered = wanted.toLowerCase();
    for (let index = 0; index < options.length; index += 1) {
      if (normalizeLabel(options[index].textContent).toLowerCase() === lowered) {
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
    /** Force an immediate classification pass, skipping the settle debounce. */
    refresh: () => {
      if (settleTimer !== null) {
        window.clearTimeout(settleTimer);
        settleTimer = null;
      }
      classify();
    },
  });

  document.addEventListener("mousedown", onMouseDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  // Capture phase, so a select inside an internally scrolling container counts.
  window.addEventListener("scroll", () => pinAll(true), true);
  window.addEventListener("resize", () => pinAll(true));

  function start() {
    startObserving();
    scheduleClassify();
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
