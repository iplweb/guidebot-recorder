(() => {
  "use strict";

  // Role gating (Spec A). Capture whether we are the top-level window BEFORE any
  // frame-bust neutralization can shadow `window.top`. Inside the framed site
  // (isTop === false) the shell already owns the cursor, so the legacy cursor
  // must NOT mount a duplicate. In the shell and in top-level popup documents the
  // cursor mounts as usual (the shell drives it through the same API).
  const isTop = window === window.top;
  if (!isTop) {
    return;
  }

  const API_KEY = "__guidebot_cursor";
  const API_VERSION = 1;
  const CURSOR_ID = "guidebot-cursor";
  const CURSOR_SELECTOR = "[data-guidebot-cursor]";
  const MAX_Z_INDEX = "2147483647";

  // --- Cursor appearance ---------------------------------------------------
  // Values come from the YAML `config.cursor` block (injected as a global by
  // the Python Overlay); each falls back to a built-in default. The pointer
  // keeps a 3:4 aspect ratio (viewBox 24x32) unless width/height override it.
  const CFG = window.__guidebot_cursor_config || {};
  const CURSOR_WIDTH = CFG.width ?? 34;
  const CURSOR_HEIGHT = CFG.height ?? 46;
  const CURSOR_FILL = CFG.fill ?? "#ef4444"; // vivid red body
  const CURSOR_STROKE = CFG.stroke ?? "#ffffff"; // white outline → any background
  const CURSOR_GLOW = CFG.glow ?? "rgba(239,68,68,.75)"; // halo, aids tracking
  // Easing for the glide. An ease-in-out (gentle start, long settle) reads as a
  // deliberate, hand-like move rather than a snap.
  const MOVE_EASING = CFG.easing ?? "cubic-bezier(.45,.05,.25,1)";

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
    const mount = () => {
      mountScheduled = false;
      ensure();
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount, { once: true });
    }
    // A freshly opened about:blank Page can report "loading" even though its
    // DOMContentLoaded event already raced past the init script. The timer is a
    // mandatory fallback in both branches; ensure() is idempotent if the event
    // also fires.
    window.setTimeout(mount, 0);
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
    svg.setAttribute("width", String(CURSOR_WIDTH));
    svg.setAttribute("height", String(CURSOR_HEIGHT));
    svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = [
      "display:block",
      "overflow:visible",
      "pointer-events:none",
      // dark drop shadow for contrast on light backgrounds + a coloured glow
      // so the pointer stays easy to track while it is moving
      `filter:drop-shadow(0 1px 2px rgba(0,0,0,.55)) drop-shadow(0 0 7px ${CURSOR_GLOW})`,
    ].join(";");

    const pointer = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pointer.setAttribute("d", "M2 1.5 2.4 24l5.5-5.2 4.3 10 4.1-1.8-4.3-9.8 7.6-.2Z");
    pointer.setAttribute("fill", CURSOR_FILL);
    pointer.setAttribute("stroke", CURSOR_STROKE);
    pointer.setAttribute("stroke-width", "2");
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
    setImportant(cursor, "width", `${CURSOR_WIDTH}px`);
    setImportant(cursor, "height", `${CURSOR_HEIGHT}px`);
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
      `left ${duration}ms ${MOVE_EASING}, top ${duration}ms ${MOVE_EASING}`,
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
