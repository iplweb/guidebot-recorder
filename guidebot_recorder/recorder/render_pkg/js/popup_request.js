(() => {
  const KEY = "__GUIDEBOT_REQUEST_KEY__";
  const OPENED = "__GUIDEBOT_OPENED_KEY__";
  if (Object.prototype.hasOwnProperty.call(window, KEY)) return;
  const native = window.open;
  if (typeof native !== "function") return;
  window[KEY] = null;
  window[OPENED] = false;
  // Captured before chrome.js shadows ``top`` (frame-bust neutralization), so
  // this is the *real* top document even from inside the shell's site iframe.
  // Publishing the record there lets the Python side read one frame instead of
  // interrogating every ad iframe on the page.
  const realTop = window.top;
  const parse = (features) => {
    if (typeof features !== "string") return null;
    const sizes = {};
    for (const part of features.split(",")) {
      const eq = part.indexOf("=");
      if (eq < 0) continue;
      const value = Number(part.slice(eq + 1).trim());
      if (Number.isFinite(value)) sizes[part.slice(0, eq).trim().toLowerCase()] = value;
    }
    const width = sizes.width !== undefined ? sizes.width : sizes.innerwidth;
    const height = sizes.height !== undefined ? sizes.height : sizes.innerheight;
    if (!(width > 0) || !(height > 0)) return null;
    return { width: Math.round(width), height: Math.round(height) };
  };
  window.open = function (...args) {
    window[OPENED] = true;
    try {
      if (realTop && realTop !== window) realTop[OPENED] = true;
    } catch (e) {}
    const requested = parse(args[2]);
    if (requested) {
      window[KEY] = requested;
      // Best effort: a genuinely cross-origin opener frame cannot write to the
      // top document, and that is fine — the Python side falls back to a
      // bounded per-frame scan.
      try {
        if (realTop && realTop !== window) realTop[KEY] = requested;
      } catch (e) {}
    }
    return native.apply(this, args);
  };
})();
