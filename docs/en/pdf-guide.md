# Building step-by-step PDF guides

Guidebot can render a compiled scenario as a landscape PDF guide — one annotated step per page,
side-by-side with narration text. Each guide page freezes the frame at the moment that best explains
its step — for most actions the moment the step completes, for a `select` the moment its option list
is open — and overlays it with visual annotations: an arrow for the cursor movement, a frame
around the action's target, a star where the mouse clicks, and the `highlight` ellipse.

This feature is LLM-free and requires no additional dependencies beyond the compiled sidecar.

## Overview

Generate a PDF guide from an already-compiled scenario:

```bash
uv run guidebot guide scenarios/login.scenario.yaml --out out/login-guide.pdf
```

The command:

- Reads the source `login.scenario.yaml` and its compiled sidecar `login.compiled.yaml`;
- Opens a fresh Chromium browser with the scenario's configured viewport and locale;
- Steps through each action, capturing screenshots and building annotated frames;
- Exports a landscape PDF with alternating content: screenshot on the left, narration on the right.

A guide requires a successful prior `compile` step. It produces no LLM calls, TTS synthesis, or video.

## Layout and page types

A single PDF guide contains one or more pages:

- **Interactive step (click, hover, type)** — Full-width annotated screenshot (left), narration text (right).
- **Dropdown step (`select`)** — The same layout, but the frame is taken **while the option list is
  open**. See [Dropdowns](#dropdowns-select).
- **Navigation** — Page with text "Otwórz adres:" followed by the URL (`navigate` steps).
- **Section divider** — A card-style slide inserted as a visual break (`slide` steps).
- **Wait/when gates** — No page. Conditional waits and background polling produce no output.

### Annotation legend

Screenshots are overlaid with visual markers:

- **Arrow** (straight segment) — Cursor movement from the previous target to the current
  one. It runs between the frames, not through their centres. When the targets overlap or
  sit less than 12 px apart, no arrow is drawn at all.
- **Red frame** — The action's target: a click, text entry, hover, or a pick from a list.
- **Star** — Where the mouse clicks: an eight-pointed star around the cursor, with a gap
  in the middle so the cursor itself stays visible.
- **Ellipse** — A `highlight` step's mark, in the colour the scenario chose. Instead of
  the circling cursor the film shows, the guide draws the finished ellipse around the
  control or area being pointed at.

## Dropdowns (`select`)

A `select` page is photographed **mid-interaction**: the option list is unfurled, and the option the
step chooses is starred the way a `click` step's target is. It is the one action whose marks are
split across two boxes:

- the **star** on the option row — the thing the reader is being told to click, drawn exactly as on
  a `click` page;
- the **red frame** around the control itself, so the reader can see which field they are in;
- the **arrow** ending at the option row's edge rather than at the control's.

The next step's arrow starts from that row, where the reader's eye was left.

Under `mode: native` (below) there is no row, so a `select` page is marked like any other framed
action: an arrow to the control's frame and the frame itself, with no star — nothing visible is
being clicked.

This works because Guidebot injects a DOM replacement for the native option list (the same one that
makes dropdowns visible in `render` videos) — a native `<select>`'s list is drawn by the operating
system and no browser-automation tool can screenshot it. Pages that enhance their own selects
(select2, Tom Select, Chosen) already draw a DOM list, and the guide drives that one instead.

`config.selects.mode: native`, or `mode: native` on a single step, opts out: the cursor still travels
to the control and the value is still chosen, but the frame shows the collapsed control and the page
carries only the red frame — there is no list to reveal. Use it for a widget the guide cannot drive;
the error message says so and names the option it was trying to choose.

Both settings are documented in the
[scenario reference](scenario-reference.md).

A step marked `optional: true` is skipped when the list **does not contain** the wanted
option — the one case where skipping silently is right, because that is exactly what
`optional` claims. Every other dropdown failure (a click that did not change the
selection, a widget that cannot be unfurled, the overlay removed mid-step) stops the
guide even for an optional step: otherwise the page would vanish from the PDF without a
word and the defect would stay on the site.

## Narration text: `say`, `teach`, or `caption`

By default, a PDF page shows the step's narration — either from `say` for standalone steps or `teach`
for taught actions. The text is displayed as-is (without TTS synthesis).

To override the PDF text for one step, use the optional `caption:` field:

```yaml
steps:
  - teach: "Click the blue login button to proceed"
    caption: "Sign in"
```

In this example, the rendered PDF shows "Sign in" instead of the full `teach` text. The `caption` field
is ignored by `render` and has no effect on compiled videos. If omitted, Guidebot falls back to `say`
or `teach` as usual.

## Command-line options

```bash
uv run guidebot guide SCENARIO.yaml --out OUTPUT.pdf [OPTIONS]
```

| Option | Default | Meaning |
|---|---:|---|
| `--out PATH`, `-o PATH` | required | Destination `.pdf` path. Parent directories are created. |
| `--headed` | off | Show the Chromium window. |
| `--pause-on-error` | off | On error, pause and keep a headed page available for inspection. |
| `--timeout SECONDS` | `15` | Playwright action timeout. |
| `--verbose`, `-v` | off | Progress bar (as in `render`), the kind of each step, and skip notices. |

The `--timeout` value is used identically to the `compile` and `render` commands and applies
to all browser actions during guide generation.

`--headed` and `--pause-on-error` behave exactly as in `compile` and `render`: they are
diagnostic tools for when capture does not do what you expect. By default a guide is built
with no visible window, but that is not a requirement — PDF composition works in both modes.

## Current v1 limitations

The current guide feature has the following scope:

- **Single language only** — Guides use the canonical narration from `say` and `teach`; multilingual
  audio tracks and `translations` are not supported. To produce localized guides, compile and build
  separate PDFs for each locale.
- **No pop-up workflows** — A scenario that opens a pop-up window is rejected with a clear error.
  The feature detects this during the preflight phase and exits without creating output.
- **No numbered multi-step grouping** — Steps are rendered individually. Future versions may allow
  multi-step sequences to be visually grouped or numbered (e.g. "Step 1 of 5").
- **No PDF layout customization** — Margins, fonts, colors, and page dimensions are fixed.
- **`scroll` only produces its own page with text** — A `scroll` step always actually scrolls the
  page (screenshots are taken from the visible viewport, so scrolling is required for later steps
  to show the right part of the page), but it only creates its own PDF page when it also carries
  `say` or `caption`. A bare `scroll` just prepares the view for the following step.

If your scenario falls outside these limits, use `render` to produce an MP4 instead.

## Verify the compiled sidecar

Before running `guide`, make sure the scenario compiles successfully:

```bash
uv run guidebot validate scenarios/login.scenario.yaml
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
uv run guidebot guide scenarios/login.scenario.yaml --out out/guide.pdf --verbose
```

If the scenario opens a pop-up window, the `guide` command will fail at startup with a
descriptive error and exit code 2. Conditional `when:` branches are handled automatically —
if a branch's gating element is absent, the whole branch is skipped and produces no pages.
Use `compile --force` if you have edited the scenario, then try `guide` again.
