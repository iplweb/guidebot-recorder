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

