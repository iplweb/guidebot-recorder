(() => {
  "use strict";

  // The shell owns the browser chrome for the MAIN window during render: a fixed
  // bar on top (macOS dots + URL pill + lock, with a focus ring and blinking
  // caret while the address bar is "typed") and, strictly below it, the target
  // site inside a sandboxed <iframe>. The synthetic cursor is mounted separately
  // (cursor.js runs in this document too) and floats above both. This script
  // only ever runs inside the shell document served from the sentinel origin.

  const API_KEY = "__guidebot_shell";
  const BAR_SELECTOR = "[data-guidebot-shell-bar]";
  const IFRAME_ID = "guidebot-site";
  const Z_INDEX = "2147483644";

  const CFG = window.__guidebot_shell_config || {};
  const HEIGHT = CFG.height ?? 56;
  const SHOW_URL = CFG.showUrl ?? true;
  const SHOW_LOCK = CFG.showLock ?? true;
  const BAR_COLOR = CFG.barColor ?? "#f3f4f6";
  const TEXT_COLOR = CFG.textColor ?? "#374151";
  const RADIUS = CFG.radius ?? 12;
  const FOCUS_COLOR = CFG.focusColor ?? "#3b82f6";
  const SHOW_CARET = CFG.showCaret ?? true;
  const DOT_COLORS = [
    CFG.closeColor ?? "#ff5f57",
    CFG.minimizeColor ?? "#febc2e",
    CFG.maximizeColor ?? "#28c840",
  ];

  let hidden = false; // persistent suppression flag (survives ensure_shell repairs)

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, "important");
  }

  function applyBarVisibility(barNode) {
    setImportant(barNode, "display", hidden ? "none" : "block");
  }

  function lockSvg() {
    return [
      '<svg viewBox="0 0 16 16" width="14" height="14" focusable="false">',
      '<path d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.5a1 1 0 0 1 1 1v6H3V8a1 1 0 0 1 1-1h.5Zm1.5 0h4V5a2 2 0 1 0-4 0v2Z" fill="currentColor"/>',
      "</svg>",
    ].join("");
  }

  function isSecureUrl(url) {
    try {
      return new URL(url, "https://guidebot.shell/").protocol === "https:";
    } catch (_error) {
      return false;
    }
  }

  function buildBar() {
    const existing = document.querySelector(BAR_SELECTOR);
    if (existing) {
      applyBarVisibility(existing);
      return existing;
    }
    const bar = document.createElement("div");
    bar.setAttribute("data-guidebot-shell-bar", "");
    bar.setAttribute("aria-hidden", "true");
    setImportant(bar, "position", "fixed");
    setImportant(bar, "top", "0");
    setImportant(bar, "left", "0");
    setImportant(bar, "right", "0");
    setImportant(bar, "height", `${HEIGHT}px`);
    setImportant(bar, "z-index", Z_INDEX);

    const shadow = bar.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = `
      :host { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      * { box-sizing: border-box; }
      [data-bar] {
        align-items: center;
        background-color: ${BAR_COLOR};
        border-radius: ${RADIUS}px ${RADIUS}px 0 0;
        color: ${TEXT_COLOR};
        display: grid;
        grid-template-columns: 88px minmax(0, 1fr) 88px;
        height: 100%;
        padding: 0 16px;
        width: 100%;
      }
      [data-dots] { display: flex; gap: 8px; }
      [data-dot] {
        border: 1px solid rgba(0, 0, 0, .12);
        border-radius: 9999px;
        display: block;
        height: 12px;
        width: 12px;
      }
      [data-pill] {
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
      [data-pill][data-focused] {
        border-color: ${FOCUS_COLOR};
        box-shadow: 0 0 0 3px ${FOCUS_COLOR}55;
      }
      [data-lock] { display: flex; flex: 0 0 auto; opacity: .72; }
      [data-url] {
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
      [data-caret] {
        background: ${TEXT_COLOR};
        display: none;
        flex: 0 0 auto;
        height: 15px;
        width: 1.5px;
      }
      [data-pill][data-focused] [data-caret] {
        display: ${SHOW_CARET ? "block" : "none"};
        animation: guidebot-blink 1s steps(1) infinite;
      }
      @keyframes guidebot-blink { 50% { opacity: 0; } }
    `;
    shadow.appendChild(style);

    const row = document.createElement("div");
    row.setAttribute("data-bar", "");
    const dots = document.createElement("div");
    dots.setAttribute("data-dots", "");
    for (const color of DOT_COLORS) {
      const dot = document.createElement("span");
      dot.setAttribute("data-dot", "");
      dot.style.backgroundColor = color;
      dots.appendChild(dot);
    }
    row.appendChild(dots);

    if (SHOW_URL) {
      const pill = document.createElement("div");
      pill.setAttribute("data-pill", "");
      const text = document.createElement("span");
      text.setAttribute("data-url", "");
      const caret = document.createElement("span");
      caret.setAttribute("data-caret", "");
      pill.appendChild(text);
      pill.appendChild(caret);
      row.appendChild(pill);
    } else {
      row.appendChild(document.createElement("span"));
    }

    shadow.appendChild(row);
    document.documentElement.appendChild(bar);
    applyBarVisibility(bar);
    return bar;
  }

  function buildIframe() {
    let iframe = document.getElementById(IFRAME_ID);
    if (!iframe) {
      iframe = document.createElement("iframe");
      iframe.id = IFRAME_ID;
      // Containment: no `allow-top-navigation*` — a recorded click cannot make
      // the framed site blow the shell away.
      iframe.setAttribute(
        "sandbox",
        "allow-scripts allow-same-origin allow-forms allow-popups " +
          "allow-modals allow-popups-to-escape-sandbox",
      );
      document.body.appendChild(iframe);
    }
    setImportant(iframe, "position", "fixed");
    setImportant(iframe, "left", "0");
    setImportant(iframe, "top", `${HEIGHT}px`);
    setImportant(iframe, "width", "100%");
    setImportant(iframe, "height", `calc(100% - ${HEIGHT}px)`);
    setImportant(iframe, "border", "0");
    setImportant(iframe, "margin", "0");
    return iframe;
  }

  function pill(bar) {
    return bar.shadowRoot?.querySelector("[data-pill]") ?? null;
  }

  function urlText(bar) {
    return bar.shadowRoot?.querySelector("[data-url]") ?? null;
  }

  function syncLock(bar, url) {
    const host = pill(bar);
    const text = urlText(bar);
    if (!host || !text) {
      return;
    }
    const existing = host.querySelector("[data-lock]");
    if (SHOW_LOCK && isSecureUrl(url)) {
      if (!existing) {
        const lock = document.createElement("span");
        lock.setAttribute("data-lock", "");
        lock.innerHTML = lockSvg();
        host.insertBefore(lock, text);
      }
    } else if (existing) {
      existing.remove();
    }
  }

  const bar = buildBar();
  buildIframe();

  const api = {
    pillRect() {
      const host = pill(bar);
      if (!host) {
        return { x: 0, y: 0, width: 0, height: 0 };
      }
      const rect = host.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    },
    focusPill() {
      const host = pill(bar);
      if (host) {
        host.setAttribute("data-focused", "");
      }
    },
    blurPill() {
      const host = pill(bar);
      if (host) {
        host.removeAttribute("data-focused");
      }
    },
    clearUrl() {
      const text = urlText(bar);
      if (text) {
        text.textContent = "";
      }
    },
    appendChar(ch) {
      const text = urlText(bar);
      if (text) {
        text.textContent += String(ch);
      }
    },
    setUrl(url) {
      const value = String(url);
      const text = urlText(bar);
      if (text) {
        text.textContent = value;
      }
      syncLock(bar, value);
    },
    hide() {
      hidden = true;
      const barNode = document.querySelector(BAR_SELECTOR);
      if (barNode) {
        applyBarVisibility(barNode);
      }
      // The site iframe is intentionally left untouched — hiding the shell
      // bar must never hide or remove #guidebot-site.
    },
    show() {
      hidden = false;
      const barNode = document.querySelector(BAR_SELECTOR);
      if (barNode) {
        applyBarVisibility(barNode);
      }
    },
  };

  Object.defineProperty(window, API_KEY, {
    configurable: true,
    enumerable: false,
    writable: true,
    value: api,
  });
})();
