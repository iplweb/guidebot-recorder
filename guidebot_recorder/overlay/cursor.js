(() => {
  "use strict";

  const API_KEY = "__guidebot_cursor";
  const API_VERSION = 1;
  const CURSOR_ID = "guidebot-cursor";
  const CURSOR_SELECTOR = "[data-guidebot-cursor]";
  const MAX_Z_INDEX = "2147483647";

  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    ["ensure", "moveTo", "ripple", "highlight"].every(
      (name) => typeof previous[name] === "function",
    )
  ) {
    previous.ensure();
    return;
  }

  const existingCursor = document.querySelector(CURSOR_SELECTOR);
  const initialX = Number.parseFloat(existingCursor?.style.left ?? "");
  const initialY = Number.parseFloat(existingCursor?.style.top ?? "");
  const state = {
    x: Number.isFinite(initialX) ? initialX : 0,
    y: Number.isFinite(initialY) ? initialY : 0,
  };
  let mountScheduled = false;

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, "important");
  }

  function mountRoot() {
    return document.documentElement || document.body;
  }

  function scheduleMount() {
    if (mountScheduled) {
      return;
    }
    mountScheduled = true;
    document.addEventListener(
      "DOMContentLoaded",
      () => {
        mountScheduled = false;
        ensure();
      },
      { once: true },
    );
  }

  function addCursorGraphic(cursor) {
    let shadow = cursor.shadowRoot;
    if (!shadow) {
      shadow = cursor.attachShadow({ mode: "open" });
    }
    if (shadow.querySelector("svg")) {
      return;
    }

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 32");
    svg.setAttribute("width", "24");
    svg.setAttribute("height", "32");
    svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = [
      "display:block",
      "overflow:visible",
      "pointer-events:none",
      "filter:drop-shadow(0 1px 2px rgba(0,0,0,.55))",
    ].join(";");

    const pointer = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pointer.setAttribute("d", "M2 1.5 2.4 24l5.5-5.2 4.3 10 4.1-1.8-4.3-9.8 7.6-.2Z");
    pointer.setAttribute("fill", "#ffffff");
    pointer.setAttribute("stroke", "#111827");
    pointer.setAttribute("stroke-width", "1.6");
    pointer.setAttribute("stroke-linejoin", "round");
    svg.appendChild(pointer);
    shadow.appendChild(svg);
  }

  function styleCursor(cursor, isNew) {
    cursor.id = CURSOR_ID;
    cursor.className = "guidebot-cursor";
    cursor.setAttribute("data-guidebot-cursor", "");
    cursor.setAttribute("aria-hidden", "true");
    setImportant(cursor, "position", "fixed");
    setImportant(cursor, "left", `${state.x}px`);
    setImportant(cursor, "top", `${state.y}px`);
    setImportant(cursor, "display", "block");
    setImportant(cursor, "visibility", "visible");
    setImportant(cursor, "opacity", "1");
    setImportant(cursor, "width", "24px");
    setImportant(cursor, "height", "32px");
    setImportant(cursor, "margin", "0");
    setImportant(cursor, "padding", "0");
    setImportant(cursor, "border", "0");
    setImportant(cursor, "transform", "none");
    setImportant(cursor, "pointer-events", "none");
    setImportant(cursor, "z-index", MAX_Z_INDEX);
    setImportant(cursor, "box-sizing", "border-box");
    setImportant(cursor, "contain", "layout style paint");
    setImportant(cursor, "will-change", "left, top");
    if (isNew) {
      setImportant(cursor, "transition", "none");
    }
  }

  function ensure() {
    const root = mountRoot();
    if (!root) {
      scheduleMount();
      return null;
    }

    const cursors = Array.from(document.querySelectorAll(CURSOR_SELECTOR));
    let cursor = cursors.find((candidate) => candidate.id === CURSOR_ID) ?? cursors[0];
    for (const duplicate of cursors) {
      if (duplicate !== cursor) {
        duplicate.remove();
      }
    }

    const isNew = !(cursor instanceof HTMLElement) || !cursor.isConnected;
    if (isNew) {
      cursor = document.createElement("div");
      root.appendChild(cursor);
    }
    styleCursor(cursor, isNew);
    addCursorGraphic(cursor);
    return cursor;
  }

  function moveTo(x, y, ms = 600) {
    const targetX = Number(x);
    const targetY = Number(y);
    const requestedDuration = Number(ms);
    if (!Number.isFinite(targetX) || !Number.isFinite(targetY)) {
      throw new TypeError("cursor coordinates must be finite numbers");
    }
    if (!Number.isFinite(requestedDuration) || requestedDuration < 0) {
      throw new TypeError("cursor duration must be a non-negative finite number");
    }

    const cursor = ensure();
    state.x = targetX;
    state.y = targetY;
    if (!cursor) {
      return Promise.resolve();
    }

    const duration = requestedDuration;
    if (duration === 0) {
      setImportant(cursor, "transition", "none");
      setImportant(cursor, "left", `${targetX}px`);
      setImportant(cursor, "top", `${targetY}px`);
      return Promise.resolve();
    }

    cursor.getBoundingClientRect();
    setImportant(
      cursor,
      "transition",
      `left ${duration}ms cubic-bezier(.22,.61,.36,1), top ${duration}ms cubic-bezier(.22,.61,.36,1)`,
    );
    setImportant(cursor, "left", `${targetX}px`);
    setImportant(cursor, "top", `${targetY}px`);
    return new Promise((resolve) => window.setTimeout(resolve, duration));
  }

  function styleTransient(element, zIndex) {
    element.setAttribute("aria-hidden", "true");
    setImportant(element, "position", "fixed");
    setImportant(element, "display", "block");
    setImportant(element, "margin", "0");
    setImportant(element, "padding", "0");
    setImportant(element, "pointer-events", "none");
    setImportant(element, "z-index", zIndex);
    setImportant(element, "box-sizing", "border-box");
  }

  function removeAfterAnimation(element, animation, fallbackMs) {
    animation.addEventListener("finish", () => element.remove(), { once: true });
    animation.addEventListener("cancel", () => element.remove(), { once: true });
    window.setTimeout(() => element.remove(), fallbackMs);
  }

  function ripple() {
    if (!ensure()) {
      return false;
    }
    const root = mountRoot();
    if (!root) {
      return false;
    }

    const ring = document.createElement("div");
    ring.setAttribute("data-guidebot-ripple", "");
    styleTransient(ring, "2147483646");
    setImportant(ring, "left", `${state.x - 8}px`);
    setImportant(ring, "top", `${state.y - 8}px`);
    setImportant(ring, "width", "16px");
    setImportant(ring, "height", "16px");
    setImportant(ring, "border", "3px solid rgba(37, 99, 235, .9)");
    setImportant(ring, "border-radius", "9999px");
    root.appendChild(ring);

    const animation = ring.animate(
      [
        { opacity: 0.95, transform: "scale(.35)" },
        { opacity: 0, transform: "scale(3.25)" },
      ],
      { duration: 500, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" },
    );
    removeAfterAnimation(ring, animation, 600);
    return true;
  }

  function highlight(x, y, width, height) {
    const values = [x, y, width, height].map(Number);
    if (!values.every(Number.isFinite) || values[2] < 0 || values[3] < 0) {
      throw new TypeError("highlight bounds must be finite with non-negative size");
    }
    const root = mountRoot();
    if (!root) {
      scheduleMount();
      return false;
    }

    const box = document.createElement("div");
    box.setAttribute("data-guidebot-highlight", "");
    styleTransient(box, "2147483645");
    setImportant(box, "left", `${values[0]}px`);
    setImportant(box, "top", `${values[1]}px`);
    setImportant(box, "width", `${values[2]}px`);
    setImportant(box, "height", `${values[3]}px`);
    setImportant(box, "border", "3px solid rgba(37, 99, 235, .95)");
    setImportant(box, "border-radius", "6px");
    setImportant(box, "background", "rgba(59, 130, 246, .12)");
    setImportant(box, "box-shadow", "0 0 0 4px rgba(59, 130, 246, .16)");
    root.appendChild(box);

    const animation = box.animate(
      [
        { opacity: 0, transform: "scale(.98)" },
        { opacity: 1, transform: "scale(1)", offset: 0.2 },
        { opacity: 0, transform: "scale(1.015)" },
      ],
      { duration: 800, easing: "ease-out", fill: "forwards" },
    );
    removeAfterAnimation(box, animation, 900);
    return true;
  }

  const api = {
    __guidebotVersion: API_VERSION,
    ensure,
    moveTo,
    ripple,
    highlight,
    get position() {
      return [state.x, state.y];
    },
  };

  Object.defineProperty(window, API_KEY, {
    configurable: true,
    enumerable: false,
    writable: true,
    value: api,
  });
  ensure();
})();
