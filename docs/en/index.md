# Guidebot Recorder

Guidebot Recorder turns an ordered browser scenario written in YAML into a
repeatable training video with an animated cursor and one or more selectable TTS
narration tracks. It can also build a set of fully localized videos.

```text
scenario source          frozen browser targets          narrated video
*.scenario.yaml ──AI──▶ *.compiled.yaml ──no LLM──▶ *.mp4
```

!!! info "Which agent can compile a scenario today?"

    **Codex CLI is the only agent backend built into `guidebot compile`.** Claude,
    Gemini, OpenCode, and direct model APIs can help write the source YAML, but they
    cannot be selected as the compiler unless you implement a custom Python
    `Reasoner` and runner.

    The agent does not discover the whole route. You write the ordered pages and
    actions; during `compile`, Codex only resolves each current element description
    into structured target data. [Read the exact agent boundary](compiling-agents.md).

## What Guidebot does

1. **Validates** a closed YAML schema before opening a browser.
2. **Compiles** semantic instructions such as “the email field” into unique,
   structural Playwright targets.
3. **Freezes** those targets and independent element identities in a generated
   `*.compiled.yaml` sidecar.
4. **Renders** the flow from a fresh Chromium session without calling an LLM,
   optionally following one automatically detected pop-up lifecycle.
5. **Narrates and assembles** the recording using Edge TTS and ffmpeg, either as
   one MP4 with several audio streams or as independent localized MP4 files.

## What you provide

- a safe target environment, preferably staging;
- an ordered `*.scenario.yaml` file;
- environment variables for values that must not be committed;
- Codex CLI authentication for compilation;
- a page state that can be reproduced from a fresh browser session.

## Start here

<div class="grid cards" markdown>

-   :material-rocket-launch: **First video**

    ---

    Install the tools and build a scenario from validation through rendering.

    [Getting started](getting-started.md)

-   :material-robot: **Agents and compilers**

    ---

    See what Codex does, what is disabled, and how a custom backend would fit.

    [Compiling agents](compiling-agents.md)

-   :material-file-code: **Scenario authoring**

    ---

    Learn which files to create, generate, review, and commit.

    [Scenario files](scenario-files.md)

-   :material-book-open-variant: **Exact syntax**

    ---

    Check every supported config field, step, default, and restriction.

    [Scenario YAML reference](scenario-reference.md)

-   :material-volume-high: **Selectable audio languages**

    ---

    Record one visual flow and embed complete alternate narration tracks.

    [Multilingual audio](multilingual-audio.md)

-   :material-translate: **Localized video sets**

    ---

    Compile and render a separate complete scenario for every page locale.

    [Localized render sets](localized-render-sets.md)

</div>

## Current scope

Guidebot is beta software. The stock CLI supports Chromium, Codex CLI as the only
compiler backend, Edge TTS as the only narration adapter, and one automatically
followed pop-up lifecycle per scenario. A second or unexpected pop-up, explicit tab
switching, iframe content of any origin, automatic route discovery, recording a
scenario from manual browsing, and auto-heal are not implemented.

[Review the practical limitations and fixes](troubleshooting.md#current-limitations)
before automating a production flow.
