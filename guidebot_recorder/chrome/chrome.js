(() => {
  "use strict";

  // Role gating (Spec A). Capture whether we are the top-level window BEFORE any
  // frame-bust shadowing runs, then decide our role:
  //   - framed-site (isTop === false): neutralize common frame-busting and bail;
  //     the shell owns the bar, so the legacy padding bar must NOT mount here.
  //   - shell (top-level, sentinel origin): bail; shell.js renders its own bar.
  //   - popup-site (top-level, any other origin): keep today's legacy padding bar.
  const SHELL_ORIGIN = "https://guidebot.shell";
  const isTop = window === window.top;
  let origin = "";
  try {
    origin = window.location.origin;
  } catch (_error) {
    origin = "";
  }
  if (!isTop) {
    // `top`/`parent` are [Replaceable]; shadowing them makes idioms such as
    // `if (top !== self) top.location = ...` benign inside the framed site.
    try {
      const selfWindow = window;
      Object.defineProperty(window, "top", { configurable: true, get: () => selfWindow });
      Object.defineProperty(window, "parent", { configurable: true, get: () => selfWindow });
    } catch (_error) {
      /* a non-configurable top/parent cannot be neutralized; nothing more to do */
    }
    return;
  }
  if (origin === SHELL_ORIGIN) {
    return;
  }

  const API_KEY = "__guidebot_chrome";
  const API_VERSION = 1;
  const HOST_ID = "guidebot-chrome";
  const HOST_SELECTOR = "[data-guidebot-chrome]";
  const BASE_PADDING_ATTRIBUTE = "data-guidebot-chrome-base-padding";
  const TYPE_INTERVAL_MS = 24;
  const Z_INDEX = "2147483644";

  const CFG = window.__guidebot_chrome_config || {};
  // Bare popups (floating-window compositor): frame the popup in post-process,
  // never with the legacy in-DOM padding bar. Bail before mounting anything —
  // the cursor overlay is a separate init script and stays on the popup.
  if (CFG.barePopups) {
    return;
  }
  const SHOW_URL = CFG.showUrl ?? true;
  const HEIGHT = CFG.height ?? 56;
  const BAR_COLOR = CFG.barColor ?? "#f3f4f6";
  const TEXT_COLOR = CFG.textColor ?? "#374151";
  const RADIUS = CFG.radius ?? 12;
  const SHOW_LOCK = CFG.showLock ?? true;
  const DOT_COLORS = [
    CFG.closeColor ?? "#ff5f57",
    CFG.minimizeColor ?? "#febc2e",
    CFG.maximizeColor ?? "#28c840",
  ];

  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    ["ensure", "setUrl"].every((name) => typeof previous[name] === "function")
  ) {
    previous.ensure(window.location.href);
    return;
  }

  const state = {
    url: window.location.href,
    animationToken: 0,
    paddingRoot: null,
    basePadding: null,
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
      ensure(state.url);
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

  function reserveSpace(root) {
    if (state.paddingRoot !== root) {
      state.paddingRoot = root;
      state.basePadding =
        root.getAttribute(BASE_PADDING_ATTRIBUTE) ??
        window.getComputedStyle(root).paddingTop ??
        "0px";
    }
    // Keep the base in JS as well as the DOM marker.  Some SPA frameworks
    // rewrite <html> attributes while leaving its inline styles untouched;
    // recomputing from that already-expanded padding would grow it on ensure.
    root.setAttribute(BASE_PADDING_ATTRIBUTE, state.basePadding);
    setImportant(root, "padding-top", `calc(${state.basePadding} + ${HEIGHT}px)`);
  }

  function styleHost(host) {
    host.id = HOST_ID;
    host.setAttribute("data-guidebot-chrome", "");
    host.setAttribute("aria-hidden", "true");
    setImportant(host, "position", "fixed");
    setImportant(host, "left", "0");
    setImportant(host, "right", "0");
    setImportant(host, "top", "0");
    setImportant(host, "display", "block");
    setImportant(host, "width", "100%");
    setImportant(host, "height", `${HEIGHT}px`);
    setImportant(host, "margin", "0");
    setImportant(host, "padding", "0");
    setImportant(host, "border", "0");
    setImportant(host, "border-radius", `${RADIUS}px ${RADIUS}px 0 0`);
    setImportant(host, "background-color", BAR_COLOR);
    setImportant(host, "color", TEXT_COLOR);
    setImportant(host, "box-sizing", "border-box");
    setImportant(host, "overflow", "hidden");
    setImportant(host, "pointer-events", "none");
    setImportant(host, "z-index", Z_INDEX);
  }

  function createLock() {
    const lock = document.createElement("span");
    lock.setAttribute("data-guidebot-lock", "");
    lock.setAttribute("aria-hidden", "true");
    lock.innerHTML = [
      '<svg viewBox="0 0 16 16" width="14" height="14" focusable="false">',
      '<path d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.5a1 1 0 0 1 1 1v6H3V8a1 1 0 0 1 1-1h.5Zm1.5 0h4V5a2 2 0 1 0-4 0v2Z" fill="currentColor"/>',
      "</svg>",
    ].join("");
    return lock;
  }

  function addBarGraphic(host) {
    let shadow = host.shadowRoot;
    if (!shadow) {
      shadow = host.attachShadow({ mode: "open" });
    }
    if (shadow.querySelector("[data-guidebot-bar]")) {
      return shadow;
    }

    const style = document.createElement("style");
    style.textContent = `
      :host { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      * { box-sizing: border-box; }
      [data-guidebot-bar] {
        align-items: center;
        display: grid;
        grid-template-columns: 88px minmax(0, 1fr) 88px;
        height: 100%;
        padding: 0 16px;
        width: 100%;
      }
      [data-guidebot-dots] { display: flex; gap: 8px; }
      [data-guidebot-dot] {
        border: 1px solid rgba(0, 0, 0, .12);
        border-radius: 9999px;
        display: block;
        height: 12px;
        width: 12px;
      }
      [data-guidebot-url-pill] {
        align-items: center;
        align-self: center;
        background: rgba(255, 255, 255, .72);
        border: 1px solid rgba(0, 0, 0, .08);
        border-radius: 9px;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, .55);
        display: flex;
        gap: 7px;
        height: 34px;
        justify-self: stretch;
        min-width: 0;
        padding: 0 12px;
      }
      [data-guidebot-lock] { display: flex; flex: 0 0 auto; opacity: .72; }
      [data-guidebot-url-text] {
        direction: ltr;
        flex: 1 1 auto;
        font-size: 13px;
        line-height: 1;
        min-width: 0;
        overflow: hidden;
        text-align: center;
        text-overflow: ellipsis;
        unicode-bidi: plaintext;
        white-space: nowrap;
      }
    `;
    shadow.appendChild(style);

    const bar = document.createElement("div");
    bar.setAttribute("data-guidebot-bar", "");
    const dots = document.createElement("div");
    dots.setAttribute("data-guidebot-dots", "");
    for (const color of DOT_COLORS) {
      const dot = document.createElement("span");
      dot.setAttribute("data-guidebot-dot", "");
      dot.style.backgroundColor = color;
      dots.appendChild(dot);
    }
    bar.appendChild(dots);

    if (SHOW_URL) {
      const pill = document.createElement("div");
      pill.setAttribute("data-guidebot-url-pill", "");
      const text = document.createElement("span");
      text.setAttribute("data-guidebot-url-text", "");
      pill.appendChild(text);
      bar.appendChild(pill);
    }

    shadow.appendChild(bar);
    return shadow;
  }

  function isSecureUrl(url) {
    try {
      return new URL(url, window.location.href).protocol === "https:";
    } catch (_error) {
      return false;
    }
  }

  function syncLock(shadow, url) {
    if (!SHOW_URL) {
      return;
    }
    const pill = shadow.querySelector("[data-guidebot-url-pill]");
    const text = shadow.querySelector("[data-guidebot-url-text]");
    if (!pill || !text) {
      return;
    }
    const existing = pill.querySelector("[data-guidebot-lock]");
    if (SHOW_LOCK && isSecureUrl(url)) {
      if (!existing) {
        pill.insertBefore(createLock(), text);
      }
    } else if (existing) {
      existing.remove();
    }
  }

  function renderUrl(shadow, url) {
    if (!SHOW_URL) {
      return;
    }
    const text = shadow.querySelector("[data-guidebot-url-text]");
    if (text) {
      text.textContent = url;
    }
    syncLock(shadow, url);
  }

  function ensure(url) {
    if (typeof url === "string") {
      state.url = url;
    }
    const root = mountRoot();
    if (!root) {
      scheduleMount();
      return null;
    }
    reserveSpace(root);

    const hosts = Array.from(document.querySelectorAll(HOST_SELECTOR));
    let host = hosts.find((candidate) => candidate.id === HOST_ID) ?? hosts[0];
    for (const duplicate of hosts) {
      if (duplicate !== host) {
        duplicate.remove();
      }
    }
    if (!(host instanceof HTMLElement) || !host.isConnected) {
      host = document.createElement("div");
      root.appendChild(host);
    }
    styleHost(host);
    const shadow = addBarGraphic(host);
    renderUrl(shadow, state.url);
    return host;
  }

  function delay(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async function setUrl(url, animate = true) {
    if (typeof url !== "string") {
      throw new TypeError("chrome URL must be a string");
    }
    const host = ensure();
    state.url = url;
    const token = ++state.animationToken;
    if (!host || !SHOW_URL) {
      return;
    }
    const shadow = host.shadowRoot;
    const text = shadow?.querySelector("[data-guidebot-url-text]");
    if (!shadow || !text) {
      return;
    }
    syncLock(shadow, url);
    if (!animate) {
      text.textContent = url;
      return;
    }

    text.textContent = "";
    for (const character of url) {
      await delay(TYPE_INTERVAL_MS);
      if (token !== state.animationToken) {
        return;
      }
      text.textContent += character;
    }
  }

  const api = {
    __guidebotVersion: API_VERSION,
    ensure,
    setUrl,
  };
  Object.defineProperty(window, API_KEY, {
    configurable: true,
    enumerable: false,
    writable: true,
    value: api,
  });
  ensure(window.location.href);
})();
