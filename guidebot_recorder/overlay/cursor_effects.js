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
