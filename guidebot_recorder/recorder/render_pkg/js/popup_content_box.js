() => {
  const body = document.body;
  if (!body) return null;
  const vw = Math.round(window.innerWidth);
  const vh = Math.round(window.innerHeight);
  if (!(vw > 0) || !(vh > 0)) return null;
  // A popup that paints its own page background is full-bleed by construction:
  // the background covers the whole window, so the union of body's children is
  // only the *ink* on top of it and cropping to that would cut the popup's
  // background away. ``body``/``documentElement`` are skipped everywhere else in
  // this walk precisely because their box is the context viewport — but whether
  // they PAINT is a different question, and the honest answer here is "decline".
  // An unstyled document leaves both transparent (the white is the canvas, not a
  // background), so ordinary dialogs are unaffected.
  const paintsPage = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const bg = style.backgroundColor;
    if (bg && bg !== "transparent" && !/^rgba\(0,\s*0,\s*0,\s*0\\)$/.test(bg)) return true;
    return Boolean(style.backgroundImage) && style.backgroundImage !== "none";
  };
  if (paintsPage(document.documentElement) || paintsPage(body)) return null;
  const MAX_DEPTH = 12;
  const MAX_NODES = 1200;
  // The walk runs on the popup's main thread while the popup is on camera, so
  // it is bounded by wall clock as well as by node count: whatever the DOM
  // looks like, it cannot cost the recording more than this. Overrunning
  // abandons the measurement (partial unions are worse than none — they would
  // crop away real content), and level 3 decides instead.
  const BUDGET_MS = 20;
  const deadline = performance.now() + BUDGET_MS;
  let overran = false;
  const REPLACED = new Set([
    "IMG", "SVG", "CANVAS", "VIDEO", "IFRAME", "INPUT", "TEXTAREA",
    "SELECT", "BUTTON", "OBJECT", "EMBED", "HR",
  ]);
  let visited = 0;
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  const add = (rect) => {
    if (!rect) return;
    const l = Math.max(0, rect.left);
    const t = Math.max(0, rect.top);
    const r = Math.min(vw, rect.right);
    const b = Math.min(vh, rect.bottom);
    if (!(r > l) || !(b > t)) return;
    if (l < left) left = l;
    if (t < top) top = t;
    if (r > right) right = r;
    if (b > bottom) bottom = b;
  };
  const isOverlay = (el) => {
    for (const attr of el.attributes || []) {
      if (attr.name.startsWith("data-guidebot")) return true;
    }
    return false;
  };
  const paints = (el, style) => {
    if (REPLACED.has(el.tagName)) return true;
    const bg = style.backgroundColor;
    if (bg && bg !== "transparent" && !/^rgba\(0,\s*0,\s*0,\s*0\\)$/.test(bg)) return true;
    if (style.backgroundImage && style.backgroundImage !== "none") return true;
    if (style.boxShadow && style.boxShadow !== "none") return true;
    for (const side of ["Top", "Right", "Bottom", "Left"]) {
      if (parseFloat(style["border" + side + "Width"]) > 0) return true;
    }
    return false;
  };
  const walk = (el, depth) => {
    if (overran) return;
    if (visited++ > MAX_NODES || performance.now() > deadline) {
      overran = true;
      return;
    }
    if (isOverlay(el)) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return;
    if (Number(style.opacity) === 0) return;
    if (paints(el, style) || depth >= MAX_DEPTH) {
      add(el.getBoundingClientRect());
      return;
    }
    let descended = false;
    for (const node of el.childNodes) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        walk(node, depth + 1);
        descended = true;
      } else if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) {
        // Measure the glyphs, not the block box the text sits in: a <p> is as
        // wide as its container, its text usually is not.
        const range = document.createRange();
        range.selectNodeContents(node);
        add(range.getBoundingClientRect());
        descended = true;
      }
    }
    if (!descended) add(el.getBoundingClientRect());
  };
  for (const child of body.children) walk(child, 1);
  if (overran) return null;
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
  if (!(right > left) || !(bottom > top)) return null;
  return {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
    viewportWidth: vw,
    viewportHeight: vh,
  };
}
