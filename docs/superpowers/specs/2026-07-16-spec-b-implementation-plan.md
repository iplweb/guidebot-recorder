# Spec B — implementation plan (parallelized)

Date: 2026-07-16
Spec: `2026-07-16-floating-popup-window-design.md`
Approach: 2 file-disjoint workstreams in parallel (TDD), then one integration pass.

## Round 1 — parallel, independent

### WS-1 — PopupConfig  (`guidebot_recorder/models/config.py`)
New `PopupConfig` (pydantic v2, `ConfigDict(extra="forbid", populate_by_name=True)`),
snake + camelCase alias, defaults:
- `floating`/`floating`: bool = True
- `scale`: float = 0.72
- `corner_radius`/`cornerRadius`: int = 14
- `shadow`: bool = True
- `backdrop_dim`/`backdropDim`: float = 0.45
- `backdrop_blur`/`backdropBlur`: int = 0
- `open_ms`/`openMs`: int = 320
- `close_ms`/`closeMs`: int = 240

Nests: `Config.popup: PopupConfig = Field(default_factory=PopupConfig)`. **Not** in
`config_hash` (cosmetic, like `CursorConfig`); no `CONFIG_HASH_VERSION` bump.
Tests: alias + default parsing, `extra="forbid"`, `config_hash` unchanged when any
popup field changes.
Files: `models/config.py`, `tests/unit/models/test_config.py`.

### WS-2 — floating compositor  (`guidebot_recorder/video/mux.py`)
Extend `compose_popup_video` with a floating branch. Target signature (keep existing
positional params; add keyword-only):
```
compose_popup_video(main, popup, out, opened_at, closed_at, *,
    visual_ready_delay=0.0, floating=False, scale=0.72, corner_radius=14,
    shadow=True, backdrop_dim=0.45, backdrop_blur=0, open_ms=320, close_ms=240,
    hold_open_at_end=False)
```
- `floating=False` → **today's exact filtergraph, byte-for-byte behavior** (early
  branch; reuse the shared validation/trim math above the concat assembly).
- `floating=True` → CFR-normalize main (`fps`) before splitting; **3-way** split
  (mid = `main[opened_at:closed_at]`, always consumed); mid = `overlay(dim(main_mid),
  framed_scaled_faded(popup_cut))`; concat `pre? + mid + tail?`. Handle no-pre/no-tail
  (route `[0:v]` straight into dim→overlay — the `len(labels)==1→null` shortcut must
  NOT apply). Rounded corners via `format=rgba`+`geq` alpha; shadow = pre-blurred
  rounded black rect behind; `fade` in/out with `open_eff=min(open_ms,span/2)`,
  `close_eff=min(close_ms,span-open_eff)`; dim ramped over `open_eff`; skip close
  when `hold_open_at_end`. Every segment ends `setsar=1,format=yuv420p` under
  `settb=AVTB,setpts=PTS-STARTPTS`. Fail loud if the CFR mid backdrop is empty.
Tests (ffmpeg-marked): floating output duration == main_duration; composite present
in interval; empty-interval main → full-length dimmed backdrop (no error); no-pre and
no-tail degenerate cases; span < open_ms+close_ms clamps without overrun;
`floating=False` unchanged vs today.
Files: `video/mux.py`, `tests/unit/video/test_mux*.py` (or the popup compose test).

## Round 2 — integration (`render.py`, `chrome.py`, `chrome.js`)
- Bare-popup seam: `barePopups` flag in the `__guidebot_chrome_config` prelude
  (`chrome.py`) → `chrome.js` popup-site branch returns before mounting.
- Thread `expect_chrome=False` for popup pages through `_prime_visuals` (status
  script must not require `[data-guidebot-chrome]`), `_ensure_visuals` (non-shell
  branch), `_prepare_popup` (no `chrome.ensure`). Derive "is popup page" from
  `observe_page` (non-first page).
- Pass `cfg.popup` params + `hold_open_at_end=popup_open_at_end` to the
  `compose_popup_video` call site.
Tests: bare popup mounts no bar and `_prime_visuals` stabilizes; end-to-end floating
render (ffmpeg/chromium) produces a valid MP4.

## Known-good baseline
Full fast suite currently green on `main`. Do not regress. The two historical
"cli"/"click" redaction failures are fixed on branch `agent/fix-redaction-referenced-env`
(PR #7), not on this branch — they may reappear locally; ignore them here.
