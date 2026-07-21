# Building step-by-step PDF guides

Guidebot can render a compiled scenario as a landscape PDF guide — one annotated step per page,
side-by-side with narration text. Each guide page freezes the frame at the moment an interactive
step completes and overlays it with visual annotations of the cursor movement, click target, and text input.

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
- **Navigation** — Page with text "Otwórz adres:" followed by the URL (`navigate` steps).
- **Section divider** — A card-style slide inserted as a visual break (`slide` steps).
- **Wait/when gates** — No page. Conditional waits and background polling produce no output.

### Annotation legend

Screenshots are overlaid with visual markers:

- **Arrow** (curved line) — Cursor movement from point A to point B.
- **Red circle** — Mouse click target.
- **Red frame** — Text entered into a field (from `enterText` or literal `teach` typing).
- **Glow** (soft halo) — Hover state on an element.

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
| `--verbose`, `-v` | off | Show page-build progress and step details. |

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
- **`select` shows no expanded dropdown** — A `select` step actually chooses the option, and the
  PDF page shows the screenshot taken **after** the choice. The native `<select>` option list is
  drawn by the operating system, so no browser-automation tool can capture it — the guide shows
  the collapsed control with its new value, never the open list.
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
