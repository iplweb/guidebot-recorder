# Popup / multi-window — design addendum (v1)

Date: 2026-07-14
Status: approved after self-review, ready to implement
Extends the main compile/render design with one automatically followed pop-up window.
Where this addendum differs from the main design (notably literal typing from
`teach`), this addendum is normative.

## Goal and user-visible acceptance

Support flows where a click opens a **separate Playwright `Page`** (for example a
login opened with `window.open`). The bot follows the pop-up, drives it, and the
final `.mp4` visibly switches to the pop-up and back to the main page.

- Window switching is **automatic**. Scenarios gain no window names, selectors, or
  switch commands.
- A literal `teach` sentence may infer `type`, for example
  `teach: "Wpisz Jan Kowalski w pole Nazwa"`. Compile freezes both the target and
  the literal value as `CachedAction.input_text`; render types that frozen value.
- Secrets and environment-dependent values are **not** supported through literal
  `teach`. They continue to use
  `enterText: {into: "pole hasła", text: "${PASSWORD}"}`. ENV expansion follows the
  main design, and the expanded value must never be written to the compiled sidecar,
  the fingerprint, narration, or logs.
- Compile rejects recognized sensitive instruction terms and sensitive DOM field
  metadata (for example `type=password` and password/OTP autocomplete values). This
  is defense in depth, not a general-purpose secret classifier; scenario authors
  remain responsible for putting every secret behind `enterText` + ENV.
  Sensitive `teach` text is rejected before verbose step output and before the
  Reasoner is called.

## Approved direction

1. **Auto-follow.** The engine tracks `main_page`, `active_page`, and at most one
   `popup_page`. `active_page` starts as main, becomes the pop-up after its opening
   click, and returns to main when the pop-up closes.
2. **One pop-up lifecycle total.** v1 accepts at most one newly created pop-up
   `Page` during the entire compile/render session—not merely one at a time. Nested,
   simultaneous, and a second sequential pop-up are unsupported and fail loudly.
3. **Cut / switch compositing.** The film shows main until the pop-up opens, the
   pop-up full-frame while it is active, then main again. Every page uses the same
   context viewport and `record_video_size`.
4. **Fail-loud replay.** An expected pop-up that does not open, an unexpected
   pop-up, a second pop-up, or loss of the main page is a compile/render error. The
   runner never silently continues on the wrong page.

## Self-review outcomes

The implementation direction was tightened around five failure modes found during
self-review:

- page events are attributed only after the actual click is armed, so a timer or ad
  opened while the Reasoner runs cannot become `opens_popup=True` accidentally;
- every render preflights the current source/config fingerprint, including a frozen
  `teach` literal, before synthesizing speech or opening a recording context;
- asynchronous close is fail-loud because compile and render spend different time
  in narration and could otherwise continue in different windows;
- returning to main settles a possible opener navigation before identity/overlay
  work, avoiding a destroyed-execution-context race;
- the shared clock starts only after a forced neutral first frame, while bounded
  popup-encoder padding handles the separate WebM startup delay without hiding a
  large timing mismatch.

## Frozen model and migration

This is a behavioral schema change. Set `COMPILER_VERSION = 2`.

`CachedAction` gains:

- `opens_popup: bool = False` — observed on the action that creates the pop-up;
- `input_text: str | None = None` — a literal value frozen only when a `teach`
  instruction resolves to `action == "type"`.

Model invariants:

- `input_text` is required for `teach` + `type` and forbidden for other actions;
- an explicit `enterText` action reads its value from the expanded source scenario
  during compile/render and never freezes that value in `CachedAction`;
- only an actual `click` action may set `opens_popup=True` in v1 (a `teach` that
  resolves to `hover` or `type` does not wait for a pop-up).

`opens_popup` is observed output metadata, not an input used to decide whether the
instruction/config fingerprint matches. Nevertheless, every real compile run must
refresh it after executing both freshly resolved and reused click actions.

Migration is strict:

- `CompiledScenario.compiler_version` and every action fingerprint must equal the
  current `COMPILER_VERSION`;
- `_can_reuse` / `compile_up_to_date` must compare at least compiler version,
  command kind, `compiled_from`, config hash, and relevant frozen state;
- render rejects an artifact with an older top-level or action version and asks for
  compile, and also rejects any per-step fingerprint that no longer matches the
  current source/config;
- loading an old artifact with the default `opens_popup=False` is parsing
  compatibility only—it must not make that artifact semantically current.

Thus an artifact produced before this addendum is recompiled once instead of
silently replaying a pop-up click on the main page.

## Window-session contract

The page context is `main_page.context`. A small window-session tracker owns the
main/active/pop-up references and the page-event bookkeeping; compile and render use
the same state-transition rules.

- The main page must remain open for the whole session.
- The pop-up must be a newly created `Page`. Reusing a pre-existing named window,
  native browser chrome, iframes, and downloads are outside v1. The optional
  synthetic `config.chrome` DOM bar is installed at context level before either
  page is created and synchronized on both pages. A page-event task immediately
  primes both visual layers and is awaited before the new page is used. The
  compositor discards any raw frames preceding that verified point, so the first
  popup frame visible in the final film contains both layers.
- Supported closure is caused by a scenario action in the pop-up, or by end of
  session when the pop-up remains open. Timer-driven/asynchronous closure between
  actions is outside v1 and fails loudly; compile and render otherwise have
  different timing because only render waits for narration. The supported action
  window includes its close-aware readiness wait, so a close triggered by an
  asynchronous click handler can still complete normally.
- Before resolving/executing a step, and again after narration or a timed wait, the
  tracker refreshes `active_page`. A pop-up closed by the preceding scenario action
  selects main; any other close fails before another step can run.
- Candidate collection, cache validation, identity capture, locators, navigation,
  waits, overlay operations, action execution, readiness, and debug pause all use
  the current active page.
- `Recorder` must be rebound to the active page (or recreated with the same overlay
  and settle configuration). A recorder permanently bound to main is invalid.
- Every newly observed page receives the configured default timeout. The context
  supplies the common viewport and, during render, the common video size; compile
  also applies the configured viewport to the supplied main page before the first
  step.

## Compile

For each step:

1. Refresh the active page. Resolve/reuse and validate the action on that page.
2. For an actual click, arm page-event observation **immediately before** executing
   the click, after target resolution and identity capture. A page observed earlier
   during reasoner work is unexpected and cannot be attributed to that click.
   After the action, allow a short bounded discovery window (approximately one
   second, measured from the actual locator click) for a newly created page. A page
   appearing after that deadline is unexpected. Ordinary non-click actions do not
   poll.
3. Compute/apply the action's `expect` readiness on the page that executed the
   action, not on the newly selected page. If that page closed during the action,
   skip page-bound readiness, switch to main, and settle/wait for main instead.
4. If exactly one pop-up appeared, apply timeout/viewport policy, wait for its load
   state (raced against premature close), set it active, and persist
   `opens_popup=True`. No pop-up persists `False`. This refresh also happens on a
   reused action.
5. If an active pop-up closes as the result of an action, return to main before the
   next step. Time-only waits use a monotonic/async sleep rather than a timeout API
   on a page that may close.

The URL heuristic uses the page that performed the action and is close-aware. A
pop-up opening is not misclassified as navigation merely because the active page
changes.

For `teach` + inferred `type`, the Reasoner returns one target and one literal text
value. Compile validates the target as editable, captures its identity, performs
the fill, and freezes the literal as `input_text`. A non-literal/placeholder-like
value or any request for a secret is rejected with guidance to use `enterText` +
ENV.

## Render and event timing

Render remains 0×LLM and treats the compiled lifecycle as a replay contract.

- Install the cursor overlay and, when enabled, synthetic `config.chrome` init
  scripts on the **BrowserContext before creating the main page**, so they apply to
  every document. A freshly opened `about:blank` can replace its initial document
  after losing init-script timers, so the context page event must also start a
  bounded Python task that retries both `ensure()` calls until the document root and
  layers remain stable for a quiescence window. Mount Chrome and cursor atomically
  in one browser task before forcing a captured frame. Retain and await the prime
  task before using the page, and record its verified-ready timestamp for assembly.
  Raw WebM may already contain an earlier paint, so assembly must trim to the
  verified point rather than clone raw frame zero. Repeat the atomic ensure after
  navigation/load and when switching pages.
- Use one monotonic recording anchor shared by narration and window events. Chromium
  may not emit a video frame for a pristine `about:blank`, so first paint and capture
  a neutral main document with the overlay, allow a bounded warm-up, and establish the
  anchor immediately afterwards. This prevents an arbitrarily long pre-navigation
  narration from living on a wall-clock interval absent from the main WebM.
- Register the context `page` listener for the whole render. It passively detects
  unexpected pages without adding a one-second wait to normal clicks.
- On the page event, immediately retain the pop-up `Video` handle, attach its close
  listener, and record `t_open` from the shared anchor. On the close event, record
  `t_close`; select main only when the close happened during a supported action.
- When `cached.opens_popup` is true, the opening click must yield that event within
  one second measured from the actual locator click—not from the preceding cursor
  animation; otherwise raise `RenderError("re-compile")`. When it is false, any new
  page event is an unexpected-lifecycle error, including after the final step.
- Readiness is close-aware exactly as in compile: it applies to the action page only
  while that page is open. A pop-up-closing action returns to and settles main.
- If the pop-up remains open at scenario end, set `t_close` to the session-end
  offset. The trailing main interval is then empty. Close the context to finalize
  both video files before asking either `Video` for its path.

## Video assembly

With no pop-up, keep the current single-video mux path.

Let `t_ready` be the verified visual-ready timestamp and `p_ready` its corresponding
offset in the raw popup file. With a pop-up, build a video-only composite
corresponding to:

```text
main[0:t_ready] ++ popup[p_ready:p_ready+(t_close-t_ready)] ++ main[t_close:end]
```

This is an FFmpeg filter concat, not a container-level "plain concat". Each present
segment is trimmed and rebased independently:

```text
trim=start=...:end=..., setpts=PTS-STARTPTS
```

Normalize the inputs to a compatible time base/pixel format and feed the resulting
segments to `concat=n=N:v=1:a=0`. Equal `record_video_size` means no scaling is
needed. Omit zero-length leading/trailing segments instead of asking FFmpeg to concat
an empty stream.

Before composing, probe both inputs and validate
`0 <= t_open <= t_close <= main_duration`. Playwright emits the popup `Page` event
before its separate encoder necessarily writes a first frame. If the popup file is
shorter than its wall-clock interval by a bounded startup gap, hold its first frame
for that gap only after discarding frames before the verified-ready point. Thus
`tpad` clones the first verified frame, never raw frame zero. The accepted gap is
the larger of two seconds or 15% of the interval; a larger disagreement is a hard
timing error. The existing approximate-sync trade-off for Playwright VFR remains in
force.

Finally probe the composite duration, build the unchanged narration bed on the same
wall-clock offsets, and mux one video stream plus one audio stream to the output
`.mp4`. Implementation may use one combined FFmpeg invocation or an intermediate
video, but it must avoid an accidental second lossy encode when a single encode is
practical.

## Tests and acceptance gates

### Model and migration

- `CachedAction` validates the `opens_popup` and `input_text` invariants.
- A v1/top-level-old compiled sidecar is not up to date and render rejects it.
- A literal `teach` + `type` round-trips its frozen `input_text`.
- Editing that literal after compile makes render fail preflight instead of typing
  the stale value; targetless sidecars also require current version and alignment.
- An `enterText` ENV secret is executed but absent from the compiled YAML and logs.

### Compile / lifecycle

- A repository fixture has a visually distinct main page whose button uses
  `window.open` to open a distinct page with an editable field and a close button.
- The mocked Reasoner asserts that the pop-up-only field is actually present in the
  candidate list; after the opening click, `opens_popup=True`.
- A second compile reuses cached targets with zero Reasoner calls, still executes the
  opening click, follows the pop-up, and refreshes the observed lifecycle bit.
- A pop-up-closing click returns control to a main-only target without a closed-page
  readiness error, including when the close triggers opener navigation.
- Missing, unexpected, nested/simultaneous, and second sequential pop-ups fail
  loudly. Timer-driven close during narration fails loudly. A never-closed pop-up
  is supported through end of session.

### Overlay / render / video

- The context-installed overlay exists in main and pop-up, including after each
  page navigates to a replacement document.
- Expected-pop-up-missing and unexpected-pop-up render paths raise `RenderError`
  instead of acting on a matching element in the wrong window.
- A compositor unit test uses synthetic, solid-color inputs and samples frames to
  prove the output order main → pop-up → main, the expected duration, and removal of
  pre-prime popup frames. A recorded `about:blank` replacement regression verifies
  that the first popup frame visible in the composite has both visual layers.
  Assertions only on file existence or `duration > 0` are insufficient.
- End-to-end compile → render produces one `.mp4` with exactly one video and one
  audio stream. Its sampled frames visibly include all three intervals, and its
  duration/audio placement stay within the documented VFR tolerance.
- An end-to-end scenario uses automatic switching only: opening `teach`, literal
  typing `teach` with frozen `input_text`, closing action, then a main-only action.
