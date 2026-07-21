# Compiling agents

## Short answer

**`guidebot compile` and `guidebot compile-set` currently support Codex CLI only.**
The Python architecture has a `Reasoner` extension point, but the CLI has no backend
selector and no other adapter is included.

| Role or backend | Works now? | Status |
|---|---:|---|
| Human author | Yes | Writes the ordered source scenario. |
| Any external coding/browser agent | Yes, for authoring | May create YAML; Guidebot does not integrate with it. |
| Codex CLI | Yes | Only backend wired into `compile` and `compile-set`. |
| Custom Python `Reasoner` | Programmatic only | Requires an adapter and a custom runner. |
| Claude / Claude Code | No built-in support | No adapter and no CLI switch. |
| Gemini | No built-in support | No adapter and no CLI switch. |
| OpenCode | No built-in support | No adapter and no CLI switch. |
| Direct OpenAI or other model API | No built-in support | Requires a custom adapter. |
| Ollama / LM Studio | No built-in support | Guidebot does not expose Codex local-model flags. |
| Playwright Chromium | Yes, but not an agent | Owns page inspection, validation, and execution. |

“Pluggable” therefore means **pluggable in Python**, not selectable today with
`--reasoner`, YAML config, or an environment variable.

## What the agent compiles

There are two different tasks that are easy to confuse:

1. **Author the route:** decide the pages, order, test account, narration, and
   operations, then write `steps` in `*.scenario.yaml`.
2. **Resolve targets:** on each current page, map an instruction such as “the email
   address field” to a safe structural target such as role `textbox`, name `Email`.

Guidebot only uses AI for task 2. A `navigate` step is a direct Playwright `goto` and
never calls the reasoner. There is no `discover`, `record`, or “explore this site and
invent a tutorial” command.

Render-only `config.chrome` and the optional `navigate.type` animation override are
never sent to Codex. Compile executes either navigation form directly without the
synthetic bar.

!!! example "Who may generate the source route?"

    You may ask any external agent — Codex, Claude, Gemini, or another browser/coding
    agent — to draft a scenario file. That is ordinary file authoring outside
    Guidebot. Always review the order and side effects, substitute secrets, then run
    `guidebot validate` and compile it with a supported backend.

A useful request for an external YAML-authoring agent should include:

- the application URL and expected end state;
- the required operation order, or explicit permission to investigate it;
- a ban on placing secrets directly in the file;
- exactly one main command per step;
- the one-pop-up lifecycle and no-iframe constraints;
- a request for `*.scenario.yaml` only, never a hand-written sidecar.

## What Codex sees

For a target step, Guidebot extracts at most a compact set of current-page candidates
and sends Codex:

- the trusted author instruction;
- candidate ID, semantic role, accessible name, HTML tag;
- bounding box, visibility, enabled state, and short ancestry.

Form-field values are not included directly. Page text is explicitly marked as
untrusted, but a value that the application later reflects into visible text or an
accessible name can appear in a later snapshot. Codex must return one framed JSON
object containing either:

- an `action` and structural `target`; or
- `no_action`, `multiple_actions`, or `no_handle` with an error message.

Codex does **not** click, type, navigate, inspect files, or control the browser.
Playwright builds the locator from trusted fields, requires one match, validates the
element, freezes its identity, and performs the action.

## Constrained Codex invocation

The built-in reasoner runs `codex exec`:

- ephemerally in a temporary directory;
- with a read-only sandbox and no approvals;
- with user config and rules ignored;
- with web search disabled;
- with shell, browser/computer-use, apps, plugins, skills-related installation,
  MCP-style extensions, and multi-agent support disabled;
- with a 60-second timeout per `codex exec` attempt and up to two attempts for
  malformed output; DOM-validation retries can start further attempts.

This means named Codex agents, subagents, skills, plugins, MCP servers, Browser Use,
Computer Use, and repository instructions are **not available inside stock Guidebot
compilation**. The constrained resolver is deliberately a text-to-data component.

## Authentication and model choice

Install and authenticate Codex before compiling:

```bash
npm install -g @openai/codex
codex login
codex login status
```

Guidebot reuses the stored Codex CLI session. Codex supports ChatGPT sign-in and
API-key authentication.

Guidebot does not pass `--model`, does not expose a model option, and starts Codex
with `--ignore-user-config`. Consequently:

- there is no Guidebot-supported way to pin a named model today;
- changing the model in `~/.codex/config.toml` does not select it for this call;
- documentation and CI should not assume a specific Codex model version.

The deterministic boundary begins at the reviewed `*.compiled.yaml` artifact, not at
the model response itself.

## When Codex is called

| Step | AI during compile? | Notes |
|---|---:|---|
| `say` | No | Narration only. |
| `navigate` | No | Direct URL navigation. |
| `wait: 2` | No | Direct time pause. |
| `teach` | Yes on cache miss | May resolve to click/hover or freeze a safe literal `type`; use explicit commands when the action must be fixed. |
| `click` / `hover` | Yes on cache miss | Action is fixed; target is resolved. |
| `enterText` | Yes on cache miss | Resolves `into`; never sends `text` directly, though later reflected page text can enter a snapshot. |
| `select` | Yes on cache miss | Resolves `from`; `option` is validated against the resolved control's own list, not sent to the reasoner. |
| `highlight` | Yes on cache miss | Resolves `what`; the step never touches the page. |
| conditional `wait` | Yes on cache miss | Resolves `until`. |
| any reusable target step | No | Static fast-path reuse opens no browser; during an incremental run live identity is checked before reuse. |
| `compile-set` | Per stale target | Applies the same resolver independently to each stale localized scenario. |
| `render` / `render-set` | Never | Uses only reviewed compiler-v2 sidecars. |

## Literal typing and pop-ups in compiler v2

For a non-sensitive demonstration value, `teach` may infer `type`. The reasoner must
copy a nonempty literal that appears exactly in the trusted instruction. Guidebot
stores it as `input_text` in the sidecar and replays it during render. Instructions
that mention passwords, passcodes, tokens, API keys, credentials, or similar secrets
are rejected before the reasoner call; a password-like DOM target is rejected too.
Use `enterText.text: "${ENV_VAR}"` for sensitive or replaceable values.

When a resolved click opens a new Playwright page, Guidebot observes it, marks
`opens_popup: true`, and continues compilation on that page automatically. There is
no agent-generated “switch window” step. Compiler v2 supports one pop-up lifecycle
per scenario and fails on a second, simultaneous, unexpected, or non-click pop-up.

## Custom `Reasoner`

The extension contract is asynchronous:

```python
from guidebot_recorder.resolver.page_context import Candidate
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult


class MyReasoner:
    async def resolve(
        self,
        instruction: str,
        candidates: list[Candidate],
    ) -> ReasonerResult | ReasonerError:
        # Call your model or deterministic resolver here.
        # Return validated Guidebot target data; never operate the page here.
        raise NotImplementedError
```

Pass the instance to the Python compiler runner:

```python
from pathlib import Path

from playwright.async_api import async_playwright

from guidebot_recorder.recorder.compile import run_compile_in_browser


async def compile_with_custom_reasoner() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            await run_compile_in_browser(
                Path("scenarios/login.scenario.yaml"),
                browser,
                MyReasoner(),
            )
        finally:
            await browser.close()
```

A production adapter must return `ReasonerResult`/`ReasonerError`, create one of the
supported structural target types, handle timeouts and malformed output, avoid
leaking values, and leave all page actions to Guidebot. `run_compile_in_browser`
creates the locale-matched context used by the stock CLI. This code does not make the
backend selectable through `guidebot compile`; adding a CLI/config selection layer is
separate work.

The same instance works for a localized render set: pass it to `run_compile_set`
instead, alongside a manifest loaded with `load_render_set`.

```python
from guidebot_recorder.recorder.render_set import run_compile_set
from guidebot_recorder.scenario.render_set import load_render_set

plan = load_render_set("scenarios/login.render-set.yaml")
await run_compile_set(plan, browser, MyReasoner())
```

`run_compile_set` applies the resolver independently to each stale variant, in
manifest order, exactly as `guidebot compile-set` does with `CodexReasoner`.
