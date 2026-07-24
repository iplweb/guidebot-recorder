  // --- Classification pass and observer --------------------------------------

  /**
   * Every `<select>` in the document, descending into **open** shadow roots.
   *
   * Closed roots are unreachable by design and stay native. Each root found is
   * also handed to the observer, so a select added to it later is classified
   * like any other. A root attached *after* this sweep produces no mutation
   * record of its own (`attachShadow` is invisible to a MutationObserver); it is
   * picked up by the next pass the host's own mutations trigger.
   *
   * The `"*"` sweep is the price of that coverage, and it stays: a shadow root
   * is reachable only through its host's `shadowRoot` property, which no
   * selector can express, so the native `querySelectorAll("select")` this
   * replaced cannot see into one. Passes are debounced by `settle_ms` and
   * happen only on mutation, so the walk is not per frame. `localName` rather
   * than `tagName.toLowerCase()` keeps it from allocating a string per element.
   */
  function collectSelects() {
    const found = [];
    const pending = [document];
    while (pending.length > 0) {
      const root = pending.pop();
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) {
          pending.push(element.shadowRoot);
          observeRoot(element.shadowRoot);
        }
        if (element.localName === "select") {
          found.push(element);
        }
      }
    }
    return found;
  }

  // --- The pending-pass barrier ----------------------------------------------
  //
  // `ready` answers "has the *first* pass run?" — once, and never again. That is
  // the wrong question for a step that drives a select the page has only just
  // added: the observer has already armed a pass, the select is not classified
  // yet, and `ready` settled seconds ago at page load. A caller that trusts it
  // finds a bare `<select>` with no DOM list to unfurl, and the step fails with
  // "the shim did not cover it" — for a select the shim was about to cover.
  //
  // `settled()` answers the question that actually gates such a step: "is a
  // classification pass still owed?".

  /** Resolvers waiting for the pass that was owed when they asked. */
  let passWaiters = [];

  function notifyPassComplete() {
    if (passWaiters.length === 0) {
      return;
    }
    const waiting = passWaiters;
    passWaiters = [];
    for (const resolve of waiting) {
      resolve();
    }
  }

  function classificationPending() {
    return settleTimer !== null || deadlineTimer !== null || guaranteedTimer !== null;
  }

  /**
   * A promise for the classification pass that is owed *right now*.
   *
   * Snapshot semantics, deliberately not "resolve once the page is quiescent":
   * a document that keeps mutating is never quiescent — our own `cursor.js`
   * rewrites `left`/`top` every frame of a glide — so a barrier waiting for that
   * would hang every step instead of unblocking the one that needed it. Waiting
   * for the pass owed at the moment of asking is bounded by the debounce's own
   * ceiling (`MAX_DEFERRAL_FACTOR` settle windows) no matter what the page does
   * afterwards, which is the same bound `Selects.ready_timeout` already derives.
   *
   * With nothing owed this is `ready`, so the first-pass barrier is still taken
   * on a page that has not classified anything yet.
   */
  function settled() {
    if (!classificationPending()) {
      return ready;
    }
    return new Promise((resolve) => passWaiters.push(resolve));
  }

  function classify() {
    if (!document.body) {
      // Nothing to append to yet; retry on the uncancellable timer rather than
      // resolving `ready` early or handing the retry to the starvable debounce.
      scheduleGuaranteedPass();
      return;
    }
    // Everything below runs against page-controlled DOM, so a page getter, a
    // patched prototype or a hostile `getComputedStyle` can throw anywhere in
    // it. The `finally` is what keeps such a throw cosmetic: without it the pass
    // takes `markReady()` down with it, and once the guaranteed timer has fired
    // there is nothing left to resolve `ready` — compile and render then block
    // on it until their own timeout.
    try {
      for (const select of Array.from(shims.keys())) {
        if (!isShimmable(select)) {
          unshim(select);
          continue;
        }
        // An SPA that replaces <body> takes our overlays with it while the
        // select survives; re-attach rather than losing the shim.
        const entry = shims.get(select);
        if (!entry.button.isConnected) {
          document.body.appendChild(entry.button);
        }
        if (!entry.list.isConnected) {
          document.body.appendChild(entry.list);
        }
        // Rows read stale text until the next `open()` otherwise; the page may
        // have swapped the whole option set in the meantime.
        syncOptions(entry);
        updateLabel(entry);
      }
      for (const select of collectSelects()) {
        if (shims.has(select) || !isShimmable(select)) {
          continue;
        }
        shim(select);
      }
    } finally {
      scheduleFrame();
      markReady();
      // A pass has now completed, so the fallback timer has served its purpose;
      // from here the capped debounce is enough to keep classification alive.
      if (guaranteedTimer !== null) {
        window.clearTimeout(guaranteedTimer);
        guaranteedTimer = null;
      }
      // Inside the `finally` for the same reason `markReady` is: a pass that
      // throws still has to release the callers blocked on it, or one page
      // getter takes compile and render down with it.
      notifyPassComplete();
    }
  }

  /** Run a pending pass now and retire both timers that could still start one. */
  function runPendingClassify() {
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
      settleTimer = null;
    }
    if (deadlineTimer !== null) {
      window.clearTimeout(deadlineTimer);
      deadlineTimer = null;
    }
    classify();
  }

  /**
   * Debounced classification, with a hard ceiling on how long it may be deferred.
   *
   * Every re-arm restarts the settle window, so a page that mutates constantly
   * would postpone the pass forever. The ceiling that prevents that is a *second,
   * independent* timer, armed once when the chain starts and never cancelled by
   * a re-arm — exactly what `scheduleGuaranteedPass` does for the first pass.
   *
   * Implementing the ceiling by shortening the re-armed debounce instead (clear +
   * set, with the wait clamped to the remaining budget) does not hold: a page
   * running `setTimeout(fn, 0)` re-queues its own work ahead of every freshly set
   * timer, so the ceiling never comes due. Measured that way, a select added
   * during such a storm was still unshimmed after 5 s.
   */
  function scheduleClassify(delay) {
    const requested = delay === undefined ? SETTLE_MS : delay;
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
    }
    settleTimer = window.setTimeout(() => {
      settleTimer = null;
      runPendingClassify();
    }, requested);
    if (deadlineTimer === null) {
      deadlineTimer = window.setTimeout(() => {
        deadlineTimer = null;
        runPendingClassify();
      }, Math.max(requested, SETTLE_MS * MAX_DEFERRAL_FACTOR));
    }
  }

  /**
   * The first pass, on a timer nothing cancels — not the debounce above, not
   * `refresh()`. `ready` is the barrier compile and render block on, so it must
   * settle even when the page never stops mutating.
   *
   * It fires at the same ceiling the debounce is capped at, so on a quiet page
   * the debounce still runs the first pass one settle window in — the head start
   * a widget library needs — and only a churning page falls back to this.
   */
  function scheduleGuaranteedPass() {
    if (guaranteedTimer !== null) {
      return;
    }
    guaranteedTimer = window.setTimeout(() => {
      guaranteedTimer = null;
      classify();
    }, SETTLE_MS * MAX_DEFERRAL_FACTOR);
  }

  const OWN_ATTRIBUTES = [
    BUTTON_ATTRIBUTE,
    LIST_ATTRIBUTE,
    OPTION_ATTRIBUTE,
    OPTGROUP_ATTRIBUTE,
    FOR_ATTRIBUTE,
  ];

  function isOurNode(node) {
    const element = asElement(node);
    if (!element) {
      return false;
    }
    // Option rows are checked by their own attributes as well as by ancestry:
    // a *removed* row is already detached, so `closest` no longer finds the list.
    for (const name of OWN_ATTRIBUTES) {
      if (element.hasAttribute(name)) {
        return true;
      }
    }
    return element.closest(OVERLAY_SELECTOR) !== null;
  }

  /**
   * Ignore the mutations the shim itself causes. Without this the observer would
   * re-arm the settle timer on every re-pin and classify forever.
   */
  function isRelevant(records) {
    for (const record of records) {
      // A MutationObserver cannot unobserve a single node, so a shadow root the
      // page has thrown away keeps delivering records forever. Nothing inside it
      // can reach the document any more, so reacting would only re-arm the
      // settle debounce on behalf of a dead subtree — measured, that pushed a
      // live select's shim from 0.20 s out to the 0.42 s deferral ceiling.
      if (!record.target.isConnected) {
        continue;
      }
      if (record.type === "attributes") {
        if (isOurNode(record.target)) {
          continue;
        }
        return true;
      }
      if (isOurNode(record.target)) {
        continue;
      }
      const touched = [...record.addedNodes, ...record.removedNodes].filter(
        (node) => node.nodeType === 1,
      );
      if (touched.length === 0) {
        continue;
      }
      if (touched.every(isOurNode)) {
        continue;
      }
      return true;
    }
    return false;
  }

  // The same settle_ms debounce as the first pass, so the shim never wins a race
  // against the page's own widget initialisation.
  const observer = new MutationObserver((records) => {
    if (isRelevant(records)) {
      scheduleClassify();
    }
  });

  const OBSERVER_OPTIONS = {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "style", "hidden", "multiple", "size", "disabled"],
  };

  /**
   * Observe a root once. Shadow roots need their own registration.
   *
   * Weak on purpose: this bookkeeping only answers "already registered?", and a
   * strong `Set` would pin every shadow root the page ever attached for the
   * lifetime of the document. The observer's own node list holds weak references
   * (DOM standard), so once this set lets go, a detached root is collectable.
   */
  const observedRoots = new WeakSet();
  function observeRoot(root) {
    if (observedRoots.has(root)) {
      return;
    }
    observedRoots.add(root);
    observer.observe(root, OBSERVER_OPTIONS);
  }

  /**
   * Watch the `document`, not `documentElement`.
   *
   * A MutationObserver holds the *node* it was given, and `document.open()` —
   * which is what `document.write()` and Playwright's `setContent` run on —
   * replaces `documentElement` outright. Bound to that element, the observer is
   * left watching a detached tree and never reports another mutation: the shim
   * stops classifying for the life of the document, so every select the page
   * adds from then on stays bare. `document` itself is never replaced, and
   * `subtree: true` reaches everything under whichever root is current.
   */
  function startObserving() {
    observeRoot(document);
  }

  // --- Public API ------------------------------------------------------------

  function resolveSelect(element) {
    const node = asElement(element);
    if (!node) {
      return null;
    }
    if (node.tagName.toLowerCase() === "select") {
      return node;
    }
    const uid = node.getAttribute(FOR_ATTRIBUTE);
    if (!uid) {
      return null;
    }
    for (const [select, entry] of shims) {
      if (entry.uid === uid) {
        return select;
      }
    }
    return null;
  }

  function entryFor(element) {
    const select = resolveSelect(element);
    return select ? shims.get(select) || null : null;
  }

  /**
   * Index into `select.options` of the option whose label matches `label`.
   *
   * Whitespace is collapsed and trimmed on both sides; the comparison is then
   * exact. Returns -1 when absent.
   *
   * Case-insensitive matching used to be the fallback here and nowhere else:
   * compile drives through Playwright's `select_option(label=…)` and the
   * listbox path through `_OPTION_INDEX_JS`, both exact. A scenario whose
   * option label differed only in case therefore resolved one way on a shimmed
   * select and failed outright on the other two shapes — the same scenario
   * behaving differently depending on the control's shape is exactly the drift
   * this branch exists to remove.
   */
  function optionIndexFor(element, label) {
    const select = resolveSelect(element);
    if (!select) {
      return -1;
    }
    const wanted = normalizeLabel(label);
    const options = select.options;
    for (let index = 0; index < options.length; index += 1) {
      if (optionLabel(options[index]) === wanted) {
        return index;
      }
    }
    return -1;
  }

  /** Scroll the list so option ``index`` sits centred in its scroll box. */
  function scrollOptionIntoView(element, index) {
    const entry = entryFor(element);
    if (!entry) {
      return;
    }
    const row = rowFor(entry, index);
    if (!row) {
      return;
    }
    const list = entry.list;
    const target = row.offsetTop - (list.clientHeight - row.offsetHeight) / 2;
    list.scrollTop = Math.max(0, Math.min(target, list.scrollHeight - list.clientHeight));
  }

  installApi({
    ready: ready,
    isShimmed: (element) => {
      const select = resolveSelect(element);
      return !!select && shims.has(select);
    },
    buttonFor: (element) => {
      const entry = entryFor(element);
      return entry ? entry.button : null;
    },
    listFor: (element) => {
      const entry = entryFor(element);
      return entry ? entry.list : null;
    },
    isOpen: (element) => {
      const entry = entryFor(element);
      return !!entry && entry.open;
    },
    open: (element) => {
      const select = resolveSelect(element);
      if (select) {
        openList(select);
      }
    },
    close: (element) => {
      const select = resolveSelect(element);
      if (select) {
        closeList(select);
      }
    },
    optionIndexFor: optionIndexFor,
    scrollOptionIntoView: scrollOptionIntoView,
    /**
     * Hand one select back to the browser — the per-step `mode: native` hatch.
     *
     * Under a global `shim` the select the step wants native is already shimmed,
     * so opting out has to undo the shim *and* stay undone: the marker is what
     * `isShimmable` reads, so no later classification pass reshims the control
     * out from under the recorder before it sets the value directly.
     *
     * Deliberately one-way for the life of the document. A step is the only
     * caller, its choice is per-scenario and static, and a select that flipped
     * back to shimmed mid-run would be exactly the race this removes.
     */
    pinNative: (element) => {
      const select = resolveSelect(element);
      if (!select) {
        return;
      }
      select.setAttribute(NATIVE_ATTRIBUTE, "");
      unshim(select);
    },
    /**
     * Re-arm the settle debounce — deliberately *not* an immediate pass.
     *
     * The re-injection guard calls this, and injection happens at t≈0 of a
     * document (`add_init_script` plus an explicit `evaluate`, the idiom
     * `Overlay.install` sets). Classifying there would beat the page's own
     * widget initialisation, which is the race `settle_ms` exists to prevent.
     */
    refresh: () => {
      scheduleClassify();
    },
    settled: settled,
  });

  document.addEventListener("mousedown", onMouseDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  // Capture phase, so a select inside an internally scrolling container counts
  // (`scroll` does not bubble). Unforced: a scroll that moved no shim must cost
  // no style writes — the rect comparison in `pin` is the whole point, and the
  // rAF loop would re-pin the same frame anyway.
  window.addEventListener("scroll", () => pinAll(false), true);
  // Forced, unlike scroll: `layoutList` reads `window.innerHeight`, so the list
  // needs a new clamp even when the control itself has not moved.
  window.addEventListener("resize", () => pinAll(true));

  function start() {
    startObserving();
    // Both: the debounce gives the page its settle window, the fallback timer
    // guarantees the pass happens even if that window never closes.
    scheduleClassify();
    scheduleGuaranteedPass();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
  if (document.readyState !== "complete") {
    // "settle_ms after load": re-arm the debounce so late enhancement wins.
    window.addEventListener("load", () => scheduleClassify(), { once: true });
  }
})();
