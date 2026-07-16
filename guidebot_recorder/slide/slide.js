(() => {
  "use strict";

  // Role gating (Spec A). Capture whether we are the top-level window BEFORE any
  // frame-bust neutralization can shadow `window.top`. Under Spec A, context-level
  // init scripts run in EVERY frame, including the sandboxed site iframe. The card
  // belongs only to the TOP document (the shell, or a top-level popup) — it must
  // never mount a duplicate inside the framed site's own document.
  const isTop = window === window.top;
  if (!isTop) {
    return;
  }

  const API_KEY = "__guidebot_slide";
  const API_VERSION = 1;
  const CARD_ID = "guidebot-slide";
  const CARD_SELECTOR = "[data-guidebot-slide]";
  const MAX_Z_INDEX = "2147483647";

  // --- Appearance -------------------------------------------------------------
  // Values come from `window.__guidebot_slide_config` (injected by the Python
  // Slide controller, which currently hardcodes a dark theme). Each falls back to
  // a built-in default so the raw script also works when evaluated without a
  // prelude (e.g. direct JS-level tests).
  const CFG = window.__guidebot_slide_config || {};
  const BACKGROUND = CFG.background ?? "#05070d";
  const TITLE_COLOR = CFG.titleColor ?? "#f8fafc";
  const SUBTITLE_COLOR = CFG.subtitleColor ?? "#cbd5e1";
  const NOTES_COLOR = CFG.notesColor ?? "#94a3b8";
  const FONT_FAMILY =
    CFG.fontFamily ??
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    ["show", "hide", "ensure", "token"].every((name) => typeof previous[name] === "function")
  ) {
    // Same JS context already has a live API (e.g. re-injected by ensure()'s
    // health check) — reuse it as-is. Crucially this leaves the shown-token and
    // any currently-mounted card untouched.
    return;
  }

  // Monotone shown-token: 0 (falsy) until the first show(), then strictly
  // increasing. Used by the render loop to distinguish a same-document rewrite
  // (JS context — and this closure — survives, token present) from a real
  // navigation (fresh context, fresh closure, token back to 0/falsy).
  let shownToken = 0;
  // Last card passed to show()/ensure(), used by ensure() to rebuild a wiped node.
  let lastCard = null;

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, "important");
  }

  function mountRoot() {
    return document.documentElement || document.body;
  }

  function styleRoot(root) {
    root.id = CARD_ID;
    root.setAttribute("data-guidebot-slide", "");
    root.setAttribute("aria-hidden", "true");
    setImportant(root, "position", "fixed");
    setImportant(root, "inset", "0");
    setImportant(root, "display", "flex");
    setImportant(root, "flex-direction", "column");
    setImportant(root, "align-items", "center");
    setImportant(root, "justify-content", "center");
    setImportant(root, "gap", "18px");
    setImportant(root, "padding", "48px");
    setImportant(root, "box-sizing", "border-box");
    setImportant(root, "background", BACKGROUND);
    setImportant(root, "color", TITLE_COLOR);
    setImportant(root, "font-family", FONT_FAMILY);
    setImportant(root, "text-align", "center");
    setImportant(root, "margin", "0");
    setImportant(root, "border", "0");
    setImportant(root, "z-index", MAX_Z_INDEX);
    // Deliberately NOT pointer-events:none. Unlike the cursor/chrome/transient
    // layers (which set it — cursor.js/chrome.js), the card must stay
    // hit-testable: the render loop dismisses a card before any target action
    // runs, so a stray click/hover during a card-up frame should fail
    // Playwright's hit-target actionability check instead of silently landing on
    // the hidden page underneath. (Scope caveat: this only helps pointer actions
    // — locator.fill()/waitFor() have no hit-target check — so non-pointer
    // actions rely on the render loop's shown-token assertion instead.)
    setImportant(root, "opacity", "0"); // fade-in target set right after mount
  }

  function textNode(text, color, fontSize, weight) {
    const el = document.createElement("div");
    setImportant(el, "margin", "0");
    setImportant(el, "color", color);
    setImportant(el, "font-size", fontSize);
    setImportant(el, "font-weight", weight);
    setImportant(el, "line-height", "1.35");
    setImportant(el, "max-width", "min(80vw, 960px)");
    // Notes may be multi-line; textContent preserves the raw newlines, pre-wrap
    // renders them without turning them into markup.
    setImportant(el, "white-space", "pre-wrap");
    // Text is escaped as TEXT via textContent — NEVER innerHTML — so scenario
    // strings (title/subtitle/notes, author-controlled YAML) cannot inject markup.
    el.textContent = text;
    return el;
  }

  function buildCard(card) {
    const root = document.createElement("div");
    styleRoot(root);

    if (card && card.title) {
      const title = textNode(card.title, TITLE_COLOR, "clamp(28px, 5vw, 64px)", "700");
      title.setAttribute("data-guidebot-slide-title", "");
      root.appendChild(title);
    }
    if (card && card.subtitle) {
      const subtitle = textNode(card.subtitle, SUBTITLE_COLOR, "clamp(18px, 2.6vw, 32px)", "500");
      subtitle.setAttribute("data-guidebot-slide-subtitle", "");
      root.appendChild(subtitle);
    }
    if (card && card.notes) {
      const notes = textNode(card.notes, NOTES_COLOR, "clamp(14px, 1.6vw, 20px)", "400");
      notes.setAttribute("data-guidebot-slide-notes", "");
      root.appendChild(notes);
    }
    return root;
  }

  function fadeIn(node) {
    setImportant(node, "opacity", "1");
    if (typeof node.animate === "function") {
      node.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: "ease-out" });
    }
  }

  function unmount() {
    document.querySelectorAll(CARD_SELECTOR).forEach((node) => node.remove());
  }

  function mount(card) {
    const root = mountRoot();
    if (!root) {
      return null;
    }
    unmount();
    const node = buildCard(card);
    root.appendChild(node);
    fadeIn(node);
    return node;
  }

  function show(card) {
    lastCard = card ?? null;
    shownToken += 1;
    mount(lastCard);
    return shownToken;
  }

  function hide() {
    unmount();
  }

  function ensure(card) {
    if (card !== undefined) {
      lastCard = card ?? null;
    }
    const existing = document.querySelector(CARD_SELECTOR);
    if (existing && existing.isConnected) {
      return existing;
    }
    return mount(lastCard);
  }

  function token() {
    return shownToken;
  }

  const api = {
    __guidebotVersion: API_VERSION,
    show,
    hide,
    ensure,
    token,
  };

  Object.defineProperty(window, API_KEY, {
    configurable: true,
    enumerable: false,
    writable: true,
    value: api,
  });
})();
