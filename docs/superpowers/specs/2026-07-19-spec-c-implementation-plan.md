# Spec C — implementation plan (parallelized)

Date: 2026-07-19
Spec: `2026-07-19-window-slide-transitions-design.md`
Approach: 2 file-disjoint workstreams in parallel (TDD), then one small integration.

## Round 1 — parallel, independent

### WS-1 — PopupConfig transition mode  (`guidebot_recorder/models/config.py`)
Add to `PopupConfig` (keep all existing fields incl. `floating: bool = True`):
- `transition: Literal["cut", "float", "slide"] | None = None`
- `slide_ms` / `slideMs`: int = 400

Derived accessors (properties on PopupConfig):
- `effective_transition -> str`: `self.transition or ("float" if self.floating else "cut")`
  (explicit `transition` always wins).
- `is_bare -> bool`: `self.effective_transition in ("float", "slide")`.

Not in `config_hash` (all PopupConfig is render-only). Tests: transition parse +
alias `slideMs`; `effective_transition` matrix (unset→derives from floating;
explicit wins, incl. `floating=False, transition="float"`); `is_bare` matrix;
`config_hash` unchanged when transition/slide_ms change.
Files: `models/config.py`, `tests/unit/models/test_config.py`.

### WS-2 — slide compositor  (`guidebot_recorder/video/mux.py`)
1. Change `compose_popup_video` to accept `transition: Literal["cut","float","slide"]
   | None = None` (keep `floating: bool = False` for back-compat). Resolve mode:
   `mode = transition if transition is not None else ("float" if floating else "cut")`.
   `cut`→existing hard cut; `float`→existing `_compose_floating`; `slide`→new
   `_compose_slide`. Existing callers (passing `floating=...`) keep working unchanged.
2. **Hoist** the duplicated `popup_filter`/`[popup_cut]` build so all three branches
   share one string (matches spec).
3. `_compose_slide` — self-contained overlay mid + concat, same skeleton as
   `_compose_floating` (CFR-normalize main via `fps`, 3-way split, mid always
   consumed, no `null[outv]` bypass, `setsar=1,format=yuv420p` + `settb/setpts`
   discipline, post-encode duration fail-loud guard). Probe-verified mid (W,H,FPS,span
   from the sources; `D_in=min(slide_ms/1000,span/2)`, `D_out=min(slide_ms/1000,span-
   D_in)`; guard `D<=0`→term constant 1):
   ```
   prog = min( min(1, t/D_in), max(0, min(1,(span-t)/D_out)) )     # drop 2nd term if hold_open_at_end
   base = color=black:size=WxH:rate=FPS:duration=span,settb=AVTB,setpts=PTS-STARTPTS
   [base][mid_main] overlay=x='-W*prog':y=0:eof_action=pass[wmain]  # main exits left
   [wmain][pop]     overlay=x='W*(1-prog)':y=0:eof_action=pass, setsar=1,format=yuv420p[mid]
   ```
   where `mid_main = main[opened:closed]` (from the split, NOT scaled) and `pop =
   [popup_cut]` (verbatim, full-size). Concat `pre?+mid+tail?`.
Tests (ffmpeg-marked, like the float tests): slide duration == main_duration and
frame count == round(fps*main_duration), monotonic PTS; push-in frame shows a moving
main/popup boundary (both visible); mid-interval full-frame popup (centre AND border
popup, unlike float); tail-clock alignment (frame just after closed == main at that
time); `hold_open_at_end` (no push-out, full-frame popup to end); `opened_at≈0` (no
pre) renders; `slide_ms=0` renders; back-compat: `transition="cut"`/`"float"` and
`floating=True`/`False` unchanged vs today.
Files: `video/mux.py`, `tests/unit/video/test_mux.py`.

## Round 2 — integration (`render.py`)
- `bare_popups = cfg.popup.is_bare` (was `cfg.popup.floating`).
- At the `compose_popup_video` call site pass `transition=cfg.popup.effective_transition,
  slide_ms=cfg.popup.slide_ms` (in addition to the existing float params; the float
  params are ignored for cut/slide but harmless to pass).
- An integration test (chromium+ffmpeg): a `transition: slide` scenario renders a
  valid MP4, bare-popup seam intact (no `[data-guidebot-chrome]` on the popup).

## Baseline
Full suite green on `main` (Spec A+B+#7 merged). Do not regress. cut/float must stay
byte-for-byte identical.
