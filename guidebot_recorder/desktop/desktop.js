(() => {
  "use strict";

  // Role gating (Spec A), identical rationale to slide.js: context-level init
  // scripts run in every frame including the sandboxed site iframe, but the
  // desktop opener belongs only to the TOP document (the shell). Capture the
  // top-window check before any frame-bust neutralization can shadow it.
  const isTop = window === window.top;
  if (!isTop) {
    return;
  }

  const API_KEY = "__guidebot_desktop";
  const API_VERSION = 1;
  const ROOT_ID = "guidebot-desktop";
  const ROOT_SELECTOR = "[data-guidebot-desktop]";
  // Above the chrome shell (2147483644) so it covers the empty browser, but BELOW
  // the cursor (2147483647) and its click ripple ring (…646) so the arc and the
  // double-click stay visible on top of the desktop. Ties with the ripple disc
  // (…645), which is appended after this overlay, so DOM order keeps it on top.
  const Z_INDEX = "2147483645";

  const CFG = window.__guidebot_desktop_config || {};
  const BACKGROUND = CFG.background ?? "#1f3a63";
  const LABEL_COLOR = CFG.labelColor ?? "#f8fafc";
  const WINDOW_COLOR = CFG.windowColor ?? "#ffffff";
  const FONT_FAMILY =
    CFG.fontFamily ??
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

  // The icon sits where a real desktop shortcut would: top-left quadrant, not
  // centred, so the cursor's arc to it reads as crossing the desktop.
  const ICON_LEFT_PCT = CFG.iconLeftPct ?? 12;
  const ICON_TOP_PCT = CFG.iconTopPct ?? 22;
  const ICON_SIZE = CFG.iconSize ?? 96;

  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    ["show", "hide", "ensure", "iconCenter", "openWindow", "token"].every(
      (name) => typeof previous[name] === "function"
    )
  ) {
    return;
  }

  let shownToken = 0;
  let lastDesktop = null;

  function setImportant(el, property, value) {
    el.style.setProperty(property, value, "important");
  }

  function mountRoot() {
    return document.documentElement || document.body;
  }

  function styleRoot(root) {
    root.id = ROOT_ID;
    root.setAttribute("data-guidebot-desktop", "");
    root.setAttribute("aria-hidden", "true");
    setImportant(root, "position", "fixed");
    setImportant(root, "inset", "0");
    setImportant(root, "margin", "0");
    setImportant(root, "border", "0");
    setImportant(root, "box-sizing", "border-box");
    setImportant(root, "background", lastDesktop?.color || BACKGROUND);
    setImportant(root, "font-family", FONT_FAMILY);
    setImportant(root, "z-index", Z_INDEX);
    // Hit-testable like the slide card (NOT pointer-events:none): a stray target
    // action during a desktop frame should fail Playwright's actionability check
    // rather than land on the page underneath.
    setImportant(root, "overflow", "hidden");
  }

  function buildIcon(desktop) {
    const wrap = document.createElement("div");
    wrap.setAttribute("data-guidebot-desktop-icon", "");
    setImportant(wrap, "position", "absolute");
    setImportant(wrap, "left", ICON_LEFT_PCT + "%");
    setImportant(wrap, "top", ICON_TOP_PCT + "%");
    setImportant(wrap, "transform", "translate(-50%, -50%)");
    setImportant(wrap, "display", "flex");
    setImportant(wrap, "flex-direction", "column");
    setImportant(wrap, "align-items", "center");
    setImportant(wrap, "gap", "10px");
    setImportant(wrap, "width", ICON_SIZE + 48 + "px");

    const glyph = document.createElement("div");
    setImportant(glyph, "width", ICON_SIZE + "px");
    setImportant(glyph, "height", ICON_SIZE + "px");
    setImportant(glyph, "display", "flex");
    setImportant(glyph, "align-items", "center");
    setImportant(glyph, "justify-content", "center");
    // Two icon sources, both trusted (never scenario free-text): a packaged
    // built-in arrives as an SVG string in `iconSvg`; the author's own file
    // arrives as a data URL in `iconImg`. Even so the SVG is adopted via
    // DOMParser + importNode rather than innerHTML — no HTML string is ever
    // assigned, so there is no injection surface to reason about.
    if (desktop && desktop.iconImg) {
      const img = document.createElement("img");
      img.src = desktop.iconImg;
      setImportant(img, "width", "100%");
      setImportant(img, "height", "100%");
      setImportant(img, "object-fit", "contain");
      glyph.appendChild(img);
    } else if (desktop && desktop.iconSvg) {
      const parsed = new DOMParser().parseFromString(desktop.iconSvg, "image/svg+xml");
      const svg = parsed.documentElement;
      if (svg && svg.nodeName.toLowerCase() === "svg" && !parsed.querySelector("parsererror")) {
        const adopted = document.importNode(svg, true);
        setImportant(adopted, "width", "100%");
        setImportant(adopted, "height", "100%");
        glyph.appendChild(adopted);
      }
    }
    wrap.appendChild(glyph);

    if (desktop && desktop.label) {
      const label = document.createElement("div");
      label.setAttribute("data-guidebot-desktop-label", "");
      setImportant(label, "color", LABEL_COLOR);
      setImportant(label, "font-size", "15px");
      setImportant(label, "font-weight", "500");
      setImportant(label, "text-align", "center");
      setImportant(label, "line-height", "1.25");
      setImportant(label, "text-shadow", "0 1px 3px rgba(0,0,0,0.45)");
      // TEXT, never markup — the label is author YAML.
      label.textContent = desktop.label;
      wrap.appendChild(label);
    }
    return wrap;
  }

  function unmount() {
    document.querySelectorAll(ROOT_SELECTOR).forEach((node) => node.remove());
  }

  function mount(desktop) {
    const parent = mountRoot();
    if (!parent) {
      return null;
    }
    unmount();
    const root = document.createElement("div");
    styleRoot(root);
    root.appendChild(buildIcon(desktop));
    parent.appendChild(root);
    return root;
  }

  function show(desktop) {
    lastDesktop = desktop ?? null;
    shownToken += 1;
    mount(lastDesktop);
    return shownToken;
  }

  function ensure(desktop) {
    if (desktop !== undefined) {
      lastDesktop = desktop ?? null;
    }
    const existing = document.querySelector(ROOT_SELECTOR);
    if (existing && existing.isConnected) {
      return existing;
    }
    return mount(lastDesktop);
  }

  function hide() {
    unmount();
  }

  // Viewport centre of the icon glyph, in CSS px — what the Python side moves the
  // real cursor to before the double-click. Read from layout, so it honours
  // whatever ICON_*_PCT resolved to at this viewport size.
  function iconCenter() {
    const wrap = document.querySelector("[data-guidebot-desktop-icon]");
    if (!wrap) {
      return null;
    }
    const rect = wrap.getBoundingClientRect();
    // The glyph is the first child; centre on it, not on the label below.
    const glyph = wrap.firstElementChild || wrap;
    const grect = glyph.getBoundingClientRect();
    return { x: grect.left + grect.width / 2, y: grect.top + grect.height / 2 };
  }

  // Grow a faux browser window from the icon to (almost) full frame. Purely
  // cosmetic: it bridges the double-click to the real chrome shell the Python
  // side reveals by hiding this overlay once the growth is done.
  function openWindow(ms) {
    const root = document.querySelector(ROOT_SELECTOR);
    const center = iconCenter();
    if (!root || !center) {
      return false;
    }
    const win = document.createElement("div");
    win.setAttribute("data-guidebot-desktop-window", "");
    const bar = document.createElement("div");
    setImportant(bar, "height", "34px");
    setImportant(bar, "background", "#e8ebf0");
    setImportant(bar, "border-radius", "10px 10px 0 0");
    setImportant(bar, "box-shadow", "inset 0 -1px 0 rgba(0,0,0,0.08)");
    win.appendChild(bar);
    setImportant(win, "position", "absolute");
    setImportant(win, "background", WINDOW_COLOR);
    setImportant(win, "border-radius", "10px");
    setImportant(win, "box-shadow", "0 24px 80px rgba(0,0,0,0.45)");
    setImportant(win, "overflow", "hidden");
    setImportant(win, "transform-origin", "top left");
    root.appendChild(win);

    const vw = window.innerWidth;
    const vh = window.innerHeight;
    // Final window: a small even margin around the frame.
    const margin = Math.round(Math.min(vw, vh) * 0.04);
    const fromW = 8;
    const fromH = 6;
    const start = {
      left: center.x - fromW / 2,
      top: center.y - fromH / 2,
      width: fromW,
      height: fromH,
    };
    const end = {
      left: margin,
      top: margin,
      width: vw - margin * 2,
      height: vh - margin * 2,
    };
    for (const key of ["left", "top", "width", "height"]) {
      setImportant(win, key, start[key] + "px");
    }
    if (typeof win.animate === "function") {
      win.animate(
        [
          { left: start.left + "px", top: start.top + "px", width: start.width + "px", height: start.height + "px", opacity: 0.6 },
          { left: end.left + "px", top: end.top + "px", width: end.width + "px", height: end.height + "px", opacity: 1 },
        ],
        { duration: Math.max(1, ms), easing: "cubic-bezier(0.16, 1, 0.3, 1)", fill: "forwards" }
      );
    }
    for (const key of ["left", "top", "width", "height"]) {
      setImportant(win, key, end[key] + "px");
    }
    return true;
  }

  function token() {
    return shownToken;
  }

  const api = {
    __guidebotVersion: API_VERSION,
    show,
    hide,
    ensure,
    iconCenter,
    openWindow,
    token,
  };

  Object.defineProperty(window, API_KEY, {
    configurable: true,
    enumerable: false,
    writable: true,
    value: api,
  });
})();
