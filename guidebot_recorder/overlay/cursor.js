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
  // Bumped when the API gains a method: an already-injected older build passes
  // the guard below on name checks alone and would leave the page without it.
  const API_VERSION = 2;
  const CURSOR_ID = "guidebot-cursor";
  const CURSOR_SELECTOR = "[data-guidebot-cursor]";
  const MAX_Z_INDEX = "2147483647";
  const SVG_NS = "http://www.w3.org/2000/svg";

  // --- `encircle` (the `highlight` command) --------------------------------
  const ENCIRCLE_STROKE_WIDTH = 5;
  const ENCIRCLE_FADE_MS = 260;
  // Chord samples used to invert arc length. 180 keeps the error well under a
  // pixel even for a viewport-wide ellipse, and the table is built once per lap.
  const ENCIRCLE_ARC_SAMPLES = 180;

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
  // deliberate, hand-like move rather than a snap. The curve is parsed and
  // evaluated here (see solveCubicBezier) instead of being handed to the CSS
  // engine, because the glide is driven frame by frame along a curved path.
  const DEFAULT_EASING = "cubic-bezier(.45,.05,.25,1)";
  const MOVE_EASING = CFG.easing ?? DEFAULT_EASING;
  // Perpendicular arc depth as a fraction of travel distance. 0 = straight.
  const MOVE_BOW = Math.max(0, Number(CFG.bow ?? 0.12) || 0);

  // --- Arc motion tuning ---------------------------------------------------
  // Not exposed to YAML: they shape the *feel* of the curve, not its amount.
  const ARC_MIN_DISTANCE = 40; // px below which an arc reads as a twitch
  const ARC_RAMP_END = 140; // px at which the bow reaches full strength
  const ARC_MAX_BOW_PX = 90; // a screen-wide sweep must not draw a half circle

  const previous = window[API_KEY];
  if (
    previous &&
    previous.__guidebotVersion === API_VERSION &&
    ["ensure", "moveTo", "ripple", "highlight", "encircle"].every(
      (name) => typeof previous[name] === "function",
    )
  ) {
    previous.ensure();
    return;
  }

  const existingCursor = document.querySelector(CURSOR_SELECTOR);
  const initialX = Number.parseFloat(existingCursor?.style.left ?? "");
  const initialY = Number.parseFloat(existingCursor?.style.top ?? "");
  const START = Array.isArray(CFG.start) ? CFG.start : [0, 0];
  const state = {
    x: Number.isFinite(initialX) ? initialX : (Number(START[0]) || 0),
    y: Number.isFinite(initialY) ? initialY : (Number(START[1]) || 0),
    // In-flight glide, if any: the rAF handle plus a way to release the
    // pending moveTo() promise when a newer move supersedes this one.
    raf: null,
    finishMove: null,
  };
  let mountScheduled = false;
  let hidden = false; // persistent suppression flag (survives ensure())

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, "important");
  }

  // --- Motion maths --------------------------------------------------------

  /** mulberry32: tiny, fast, fully deterministic PRNG. Never Math.random(). */
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function next() {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /**
   * FNV-1a-ish hash of the rounded endpoints. Seeding from the coordinates (and
   * nothing else) is what makes a re-render frame-identical: the same move in
   * the same scenario always bows the same way.
   */
  function seedFromEndpoints(x0, y0, x1, y1) {
    let h = 0x811c9dc5;
    for (const value of [Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)]) {
      h = Math.imul(h ^ (value | 0), 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  function smoothstep(edge0, edge1, value) {
    if (value <= edge0) {
      return 0;
    }
    if (value >= edge1) {
      return 1;
    }
    const t = (value - edge0) / (edge1 - edge0);
    return t * t * (3 - 2 * t);
  }

  const CUBIC_BEZIER_PATTERN =
    /^\s*cubic-bezier\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)\s*$/;

  function parseCubicBezier(value) {
    const match = CUBIC_BEZIER_PATTERN.exec(String(value ?? ""));
    if (!match) {
      return null;
    }
    const points = match.slice(1, 5).map(Number);
    if (!points.every(Number.isFinite)) {
      return null;
    }
    // CSS constrains the x components to [0,1]; outside that the curve is not a
    // function of progress and the solver below has no single root.
    if (points[0] < 0 || points[0] > 1 || points[2] < 0 || points[2] > 1) {
      return null;
    }
    return points;
  }

  /** Build progress -> eased progress for `cubic-bezier(x1,y1,x2,y2)`. */
  function cubicBezierEasing(x1, y1, x2, y2) {
    const cx = 3 * x1;
    const bx = 3 * (x2 - x1) - cx;
    const ax = 1 - cx - bx;
    const cy = 3 * y1;
    const by = 3 * (y2 - y1) - cy;
    const ay = 1 - cy - by;
    const sampleX = (t) => ((ax * t + bx) * t + cx) * t;
    const sampleY = (t) => ((ay * t + by) * t + cy) * t;
    const slopeX = (t) => (3 * ax * t + 2 * bx) * t + cx;

    return function ease(progress) {
      if (progress <= 0) {
        return 0;
      }
      if (progress >= 1) {
        return 1;
      }
      // Newton-Raphson, with bisection as the fallback for flat segments where
      // the derivative vanishes and Newton cannot converge.
      let t = progress;
      for (let i = 0; i < 8; i += 1) {
        const error = sampleX(t) - progress;
        if (Math.abs(error) < 1e-6) {
          return sampleY(t);
        }
        const slope = slopeX(t);
        if (Math.abs(slope) < 1e-6) {
          break;
        }
        t -= error / slope;
      }
      let low = 0;
      let high = 1;
      t = progress;
      for (let i = 0; i < 32 && Math.abs(sampleX(t) - progress) > 1e-6; i += 1) {
        if (sampleX(t) < progress) {
          low = t;
        } else {
          high = t;
        }
        t = (low + high) / 2;
      }
      return sampleY(t);
    };
  }

  const EASE = (() => {
    const points = parseCubicBezier(MOVE_EASING);
    if (points) {
      return cubicBezierEasing(points[0], points[1], points[2], points[3]);
    }
    // A cosmetic misconfiguration must not abort a render — warn once and fall
    // back to the built-in curve.
    console.warn(
      `guidebot cursor: unparsable easing ${JSON.stringify(MOVE_EASING)}, ` +
        `falling back to ${DEFAULT_EASING}`,
    );
    const fallback = parseCubicBezier(DEFAULT_EASING);
    return cubicBezierEasing(fallback[0], fallback[1], fallback[2], fallback[3]);
  })();

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
    setImportant(cursor, "display", hidden ? "none" : "block");
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
    // No `paint`: it clips painting to the host's border box, cutting the
    // drop-shadow glow (which spreads ~14px past the 34x46 box).
    setImportant(cursor, "contain", "layout style");
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

  /**
   * Where the cursor is actually painted right now. During a glide this differs
   * from state.x/state.y, which already hold the *target*.
   */
  function renderedPosition() {
    const cursor = document.querySelector(CURSOR_SELECTOR);
    if (cursor) {
      const computed = window.getComputedStyle(cursor);
      const left = Number.parseFloat(computed.left);
      const top = Number.parseFloat(computed.top);
      if (Number.isFinite(left) && Number.isFinite(top)) {
        return [left, top];
      }
    }
    return [state.x, state.y];
  }

  /** Stop any in-flight glide and release its pending promise. */
  function cancelMove() {
    if (state.raf !== null) {
      window.cancelAnimationFrame(state.raf);
      state.raf = null;
    }
    const finish = state.finishMove;
    state.finishMove = null;
    if (finish) {
      finish();
    }
  }

  /**
   * Control point of the quadratic Bezier: the A->B midpoint pushed sideways.
   * Depth grows with distance, ramps in smoothly above ARC_MIN_DISTANCE (a hard
   * cutoff would pop between two near-identical moves) and stops at
   * ARC_MAX_BOW_PX.
   */
  function arcControlPoint(x0, y0, x1, y1) {
    const dx = x1 - x0;
    const dy = y1 - y0;
    const distance = Math.hypot(dx, dy);
    const midX = (x0 + x1) / 2;
    const midY = (y0 + y1) / 2;
    if (MOVE_BOW === 0 || distance <= ARC_MIN_DISTANCE) {
      return [midX, midY];
    }
    const random = mulberry32(seedFromEndpoints(x0, y0, x1, y1));
    const side = random() < 0.5 ? -1 : 1;
    const amplitude = 0.85 + random() * 0.3; // subtle variation between moves
    const ramp = smoothstep(ARC_MIN_DISTANCE, ARC_RAMP_END, distance);
    const depth = Math.min(MOVE_BOW * distance * ramp * amplitude, ARC_MAX_BOW_PX) * side;
    // unit normal of A->B
    return [midX - (dy / distance) * depth, midY + (dx / distance) * depth];
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

    // Read where we are painted *before* ensure() rewrites left/top from state,
    // so a superseded glide resumes from the pixel on screen rather than from
    // the target it never reached.
    const [startX, startY] = renderedPosition();
    cancelMove();

    const cursor = ensure();
    // The post-swap restore reads state and must get the target, never an
    // intermediate position.
    state.x = targetX;
    state.y = targetY;
    if (!cursor) {
      return Promise.resolve();
    }

    const duration = requestedDuration;
    setImportant(cursor, "transition", "none");
    if (duration === 0) {
      setImportant(cursor, "left", `${targetX}px`);
      setImportant(cursor, "top", `${targetY}px`);
      return Promise.resolve();
    }
    setImportant(cursor, "left", `${startX}px`);
    setImportant(cursor, "top", `${startY}px`);

    const [controlX, controlY] = arcControlPoint(startX, startY, targetX, targetY);

    return new Promise((resolve) => {
      let start = null;
      let fallbackTimer = 0;

      const settle = () => {
        window.clearTimeout(fallbackTimer);
        state.raf = null;
        state.finishMove = null;
        resolve();
      };
      const land = () => {
        setImportant(cursor, "left", `${targetX}px`);
        setImportant(cursor, "top", `${targetY}px`);
        settle();
      };
      // Superseded by a newer move: leave the cursor where the new move takes
      // over from and just release the awaiting caller.
      state.finishMove = settle;

      const step = (timestamp) => {
        // Progress comes from the clock, not from a frame count: a dropped
        // frame must not desynchronize us from `duration`, which Python treats
        // as authoritative.
        if (start === null) {
          start = timestamp;
        }
        const progress = Math.min(1, (timestamp - start) / duration);
        if (progress >= 1) {
          land(); // the final frame writes the target exactly
          return;
        }
        const t = EASE(progress);
        const inv = 1 - t;
        const a = inv * inv;
        const b = 2 * inv * t;
        const c = t * t;
        setImportant(cursor, "left", `${a * startX + b * controlX + c * targetX}px`);
        setImportant(cursor, "top", `${a * startY + b * controlY + c * targetY}px`);
        state.raf = window.requestAnimationFrame(step);
      };

      // rAF stops firing in a backgrounded document; without this the promise
      // would never settle and the recording would hang.
      fallbackTimer = window.setTimeout(() => {
        if (state.raf !== null) {
          window.cancelAnimationFrame(state.raf);
          state.raf = null;
        }
        land();
      }, duration + 50);
      state.raf = window.requestAnimationFrame(step);
    });
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

  function ripple(flash = false) {
    if (!ensure()) {
      return false;
    }
    const root = mountRoot();
    if (!root) {
      return false;
    }

    const click = CFG.click || {};
    const ringColor = click.color || "rgba(37, 99, 235, .9)";
    const endScale = Number.isFinite(Number(click.scale)) ? Number(click.scale) : 3.25;

    const ring = document.createElement("div");
    ring.setAttribute("data-guidebot-ripple", "");
    styleTransient(ring, "2147483646");
    setImportant(ring, "left", `${state.x - 8}px`);
    setImportant(ring, "top", `${state.y - 8}px`);
    setImportant(ring, "width", "16px");
    setImportant(ring, "height", "16px");
    setImportant(ring, "border", `3px solid ${ringColor}`);
    setImportant(ring, "border-radius", "9999px");
    root.appendChild(ring);

    const animation = ring.animate(
      [
        { opacity: 0.95, transform: "scale(.35)" },
        { opacity: 0, transform: `scale(${endScale})` },
      ],
      { duration: 500, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" },
    );
    removeAfterAnimation(ring, animation, 600);

    if (flash && click.flash) {
      const disc = document.createElement("div");
      disc.setAttribute("data-guidebot-flash", "");
      styleTransient(disc, "2147483645");
      setImportant(disc, "left", `${state.x - 8}px`);
      setImportant(disc, "top", `${state.y - 8}px`);
      setImportant(disc, "width", "16px");
      setImportant(disc, "height", "16px");
      setImportant(disc, "background", ringColor);
      setImportant(disc, "border-radius", "9999px");
      root.appendChild(disc);

      const flashAnimation = disc.animate(
        [
          { opacity: 0.55, transform: "scale(.2)" },
          { opacity: 0, transform: "scale(2)" },
        ],
        { duration: 420, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" },
      );
      removeAfterAnimation(disc, flashAnimation, 520);
    }

    return true;
  }

  /**
   * Lap an ellipse around a target, drawing a marker trail behind the cursor.
   *
   * This is what the `highlight` scenario command animates. It has nothing to do
   * with the `highlight()` function below, which draws a one-off pulsing
   * rectangle and survives only for API compatibility: the names collide, the
   * features do not.
   *
   * The loop mirrors moveTo's on purpose: progress comes from the clock (a
   * dropped frame must not desynchronize us from the duration Python treats as
   * authoritative), the handle lives in `state.raf` so `cancelMove()` can preempt
   * us, and a fallback timer settles the promise even when rAF stops firing in a
   * backgrounded document — without it a recording would hang forever.
   */
  /**
   * Angle lookup that walks an ellipse at a CONSTANT SPEED.
   *
   * Stepping the angle linearly would not: on an ellipse, equal angles cover
   * wildly unequal arcs (at rx/ry = 8 the cursor crawls around the ends and
   * bolts along the flat runs, ~5x apart), and the trail — which is drawn by
   * arc length via stroke-dashoffset — would come unstuck from the cursor
   * mid-lap. Sampling cumulative chord length once and inverting it keeps both
   * on the same clock.
   */
  function arcLengthTable(rx, ry, samples = ENCIRCLE_ARC_SAMPLES) {
    const cumulative = new Float64Array(samples + 1);
    let previousX = rx;
    let previousY = 0;
    for (let i = 1; i <= samples; i++) {
      const angle = (i / samples) * 2 * Math.PI;
      const x = rx * Math.cos(angle);
      const y = ry * Math.sin(angle);
      cumulative[i] = cumulative[i - 1] + Math.hypot(x - previousX, y - previousY);
      previousX = x;
      previousY = y;
    }
    const total = cumulative[samples];
    return (fraction) => {
      if (!(total > 0)) {
        return fraction * 2 * Math.PI;
      }
      const wanted = fraction * total;
      let low = 0;
      let high = samples;
      while (low + 1 < high) {
        const mid = (low + high) >> 1;
        if (cumulative[mid] <= wanted) {
          low = mid;
        } else {
          high = mid;
        }
      }
      const span = cumulative[high] - cumulative[low];
      const within = span > 0 ? (wanted - cumulative[low]) / span : 0;
      return ((low + within) / samples) * 2 * Math.PI;
    };
  }

  function encircle(cx, cy, rx, ry, options = {}) {
    const centreX = Number(cx);
    const centreY = Number(cy);
    const radiusX = Number(rx);
    const radiusY = Number(ry);
    if (
      ![centreX, centreY, radiusX, radiusY].every(Number.isFinite) ||
      radiusX <= 0 ||
      radiusY <= 0
    ) {
      throw new TypeError("encircle needs a finite centre and positive radii");
    }
    const laps = Math.max(1, Math.round(Number(options.loops) || 1));
    const msPerLap = Math.max(1, Number(options.msPerLap) || 900);
    const holdMs = Math.max(0, Number(options.holdMs) || 0);
    const color = options.color || CURSOR_GLOW;

    cancelMove();
    const cursor = ensure();
    const root = mountRoot();
    // The lap starts and ends at 3 o'clock — where an SVG ellipse path also
    // starts — so the trail unrolls exactly under the cursor.
    const entryX = centreX + radiusX;
    state.x = entryX;
    state.y = centreY;
    if (!cursor || !root) {
      scheduleMount();
      return Promise.resolve();
    }

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    setImportant(svg, "position", "fixed");
    setImportant(svg, "left", "0px");
    setImportant(svg, "top", "0px");
    setImportant(svg, "width", "100%");
    setImportant(svg, "height", "100%");
    setImportant(svg, "overflow", "visible");
    setImportant(svg, "pointer-events", "none");
    setImportant(svg, "z-index", "2147483645");

    const trail = document.createElementNS(SVG_NS, "ellipse");
    trail.setAttribute("data-guidebot-encircle", "");
    trail.setAttribute("cx", centreX);
    trail.setAttribute("cy", centreY);
    trail.setAttribute("rx", radiusX);
    trail.setAttribute("ry", radiusY);
    trail.setAttribute("fill", "none");
    trail.setAttribute("stroke", color);
    trail.setAttribute("stroke-width", ENCIRCLE_STROKE_WIDTH);
    trail.setAttribute("stroke-linecap", "round");
    svg.appendChild(trail);
    root.appendChild(svg);

    const perimeter =
      typeof trail.getTotalLength === "function"
        ? trail.getTotalLength()
        : Math.PI * (radiusX + radiusY);
    trail.style.strokeDasharray = `${perimeter}`;
    trail.style.strokeDashoffset = `${perimeter}`;

    setImportant(cursor, "transition", "none");
    const spinMs = laps * msPerLap;
    const angleAt = arcLengthTable(radiusX, radiusY);

    return new Promise((resolve) => {
      let start = null;
      let fallbackTimer = 0;

      const settle = () => {
        window.clearTimeout(fallbackTimer);
        state.raf = null;
        state.finishMove = null;
        const fade = svg.animate([{ opacity: 1 }, { opacity: 0 }], {
          duration: ENCIRCLE_FADE_MS,
          easing: "ease-out",
          fill: "forwards",
        });
        removeAfterAnimation(svg, fade, ENCIRCLE_FADE_MS + 100);
        // Resolve only once the trail has faded, so the next step never opens on
        // a frame that still carries this one's mark.
        window.setTimeout(resolve, ENCIRCLE_FADE_MS);
      };
      // `cancelMove` releases a superseded animation through this hook; without
      // it a newer move would leave this promise pending forever.
      state.finishMove = settle;

      const land = () => {
        setImportant(cursor, "left", `${entryX}px`);
        setImportant(cursor, "top", `${centreY}px`);
        trail.style.strokeDashoffset = "0";
        settle();
      };

      const step = (timestamp) => {
        if (start === null) {
          start = timestamp;
        }
        const elapsed = timestamp - start;
        if (elapsed >= spinMs + holdMs) {
          land(); // the final frame puts the cursor exactly back on the entry point
          return;
        }
        // Only the spin phase advances the angle; during the hold the cursor
        // rests at the entry point and the trail stands whole.
        const spun = Math.min(elapsed, spinMs);
        const laps = spun / msPerLap;
        const angle = angleAt(laps - Math.floor(laps));
        setImportant(cursor, "left", `${centreX + radiusX * Math.cos(angle)}px`);
        setImportant(cursor, "top", `${centreY + radiusY * Math.sin(angle)}px`);
        // The first lap draws the trail; later laps trace over what is there.
        trail.style.strokeDashoffset = `${perimeter * (1 - Math.min(1, spun / msPerLap))}`;
        state.raf = window.requestAnimationFrame(step);
      };

      fallbackTimer = window.setTimeout(() => {
        if (state.raf !== null) {
          window.cancelAnimationFrame(state.raf);
          state.raf = null;
        }
        land();
      }, spinMs + holdMs + 50);
      state.raf = window.requestAnimationFrame(step);
    });
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

  function hide() {
    hidden = true;
    const cursor = document.querySelector(CURSOR_SELECTOR);
    if (cursor) {
      setImportant(cursor, "display", "none");
    }
  }

  function show() {
    hidden = false;
    ensure();
  }

  const api = {
    __guidebotVersion: API_VERSION,
    ensure,
    moveTo,
    ripple,
    highlight,
    encircle,
    hide,
    show,
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
