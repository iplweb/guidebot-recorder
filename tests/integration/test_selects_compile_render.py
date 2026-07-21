"""E2E: the DOM select shim through a full compile → render cycle.

A native ``<select>`` draws its option list as an OS popup, so the recording
never shows it. The shim replaces that list with a DOM one; pages that enhanced
their own selects (select2, Tom Select) keep theirs and are driven through it.
The fixture reproduces both *patterns* rather than vendoring the libraries.

What makes this suite worth its runtime is not that the value ends up right —
``select_option`` did that before the branch, invisibly. It is that **at the
moment the option row is clicked the list is attached and visible in the DOM**,
which is asserted from inside the browser by :class:`SelectSpy` rather than
inferred from the final value.

Reasoner mocked (deterministic), TTS fake (silent mp3) — no network, no Codex.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import guidebot_recorder.resolver.validate as validate_module
from guidebot_recorder.models.config import TtsConfig, config_hash
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.recorder.compile import run_compile, run_compile_in_browser
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.recorder.render import RenderError, run_render
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.validate import build_locator, reuse_is_valid
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.selects import install_selects
from guidebot_recorder.video.mux import probe_duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe niedostępne",
    ),
]

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "selects.html"
UNDRIVEABLE_FIXTURE = FIXTURES / "selects-undriveable.html"
POPUP_FIXTURE = FIXTURES / "selects-popup-main.html"


# --- scenarios --------------------------------------------------------------
# ``settleMs: 20`` everywhere the settle window is not what is under test: the
# readiness barrier costs up to one settle window per document, and the fixture
# enhances its selects in inline markup, so it has nothing to wait for. The
# back-compat scenario deliberately omits the whole ``selects:`` block — see
# ``test_artifact_compiled_without_the_shim_still_renders_with_it``.

FOUR_CONTROLS_SCENARIO = """\
config:
  title: Listy rozwijane
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Pokażę, jak wypełnić formularz."
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX"}}
  - select: {{from: "lista województw", option: "Mazowieckie"}}
  - select: {{from: "lista miast", option: "Lublin"}}
  - select: {{from: "lista tagów", option: "pilne"}}
"""

CLICK_SCENARIO = """\
config:
  title: Klik w listę
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Rozwijam listę."
  - navigate: "{url}"
  - click: "lista formatów raportu"
  - wait: 0.4
"""

POPUP_SCENARIO = """\
config:
  title: Lista w popupie
  viewport: {{width: 640, height: 480}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  chrome: {{enabled: true, showUrl: true, typeOnNavigate: false}}
  popup: {{floating: false}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - navigate: "{url}"
  - wait: 0.4
  - click: "Otwórz okno zamówienia"
  - select: {{from: "lista sposobów dostawy", option: "Kurier"}}
  - wait: 0.4
"""

# No ``selects:`` block at all: this is what a scenario looked like before the
# branch, and it is what the back-compat claim is about.
LEGACY_SCENARIO = """\
config:
  title: Zgodność wstecz
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
steps:
  - say: "Wybieram format."
  - navigate: "{url}"
  - select: {{from: "lista formatów raportu", option: "BibTeX"}}
  - select: {{from: "lista województw", option: "Mazowieckie"}}
"""

UNDRIVEABLE_SCENARIO = """\
config:
  title: Lista bez kontrolki
  viewport: {{width: 800, height: 600}}
  tts: {{provider: fake, voice: v, lang: pl-PL}}
  selects: {{settleMs: 20, openHoldMs: 30}}
steps:
  - say: "Wybieram województwo."
  - navigate: "{url}"
  - select: {{from: "lista województw", option: "Mazowieckie"}}
"""


# --- doubles ----------------------------------------------------------------

#: instruction fragment → ``data-testid`` of the ``<select>`` it names
_SELECT_TARGETS = {
    "formatów": "format",
    "województw": "woj",
    "miast": "miasto",
    "tagów": "tagi",
    "dostawy": "dostawa",
}


class SelectReasoner:
    """Resolves every instruction in this suite to a fixed, frozen target."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, instruction, candidates):
        self.calls += 1
        if "Otwórz" in instruction:
            return ReasonerResult(
                "click", RoleTarget(role="button", name="Otwórz zamówienie", exact=True)
            )
        for fragment, testid in _SELECT_TARGETS.items():
            if fragment in instruction:
                # ``action_for`` derives the action from the command kind for both
                # ``select:`` and ``click:``, so only the target matters here.
                return ReasonerResult("select", TestidTarget(testid=testid))
        raise AssertionError(f"nieoczekiwana instrukcja: {instruction!r}")


class FakeTts:
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "0.3",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return 0.3


# --- in-browser observation -------------------------------------------------

#: Record, in the browser, what each click actually landed on.
#:
#: Capture phase is not a detail: a page's own widget removes its list in the
#: bubble-phase handler of the very click being observed, so a bubbling listener
#: would see the list already gone and could never distinguish "the list was
#: there" from "the value was set invisibly".
_PROBE_JS = """() => {
  if (window.__gbSelectProbe) return;
  window.__gbSelectProbe = [];
  document.addEventListener(
    "click",
    (event) => {
      const el = event.target;
      if (!el || !el.closest) return;
      if (el.tagName === "OPTION") {
        // A `multiple` / `size > 1` select needs no shim: its rows are laid out
        // in the page already. What has to be true at click time is that the row
        // is inside the listbox's own visible box — i.e. the viewer saw it.
        const box = el.getBoundingClientRect();
        const owner = el.closest("select").getBoundingClientRect();
        window.__gbSelectProbe.push({
          kind: "listboxOption",
          label: (el.textContent || "").replace(/\\s+/g, " ").trim(),
          visible: box.width > 0 && box.height > 0,
          insideListbox: box.top >= owner.top - 1 && box.bottom <= owner.bottom + 1,
        });
        return;
      }
      const list = el.closest("[data-guidebot-select-list], [data-fake-list]");
      if (list === null) {
        window.__gbSelectProbe.push({
          kind: "control",
          tag: el.tagName.toLowerCase(),
          id: el.id || null,
        });
        return;
      }
      const rect = list.getBoundingClientRect();
      const style = getComputedStyle(list);
      const rows = Array.from(
        list.querySelectorAll("[data-guidebot-option-index], [data-fake-option]")
      ).filter((row) => {
        const box = row.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
      });
      const shim = list.hasAttribute("data-guidebot-select-list");
      let below = null;
      if (shim) {
        const uid = list.getAttribute("data-guidebot-for");
        const owner = document.querySelector(`[data-guidebot-shimmed="${uid}"]`);
        if (owner) below = rect.top >= owner.getBoundingClientRect().bottom - 1;
      }
      window.__gbSelectProbe.push({
        kind: "option",
        label: (el.textContent || "").replace(/\\s+/g, " ").trim(),
        shim: shim,
        attached: list.isConnected,
        atBodyLevel: list.parentElement === document.body,
        visible:
          rect.width > 0 &&
          rect.height > 0 &&
          style.visibility !== "hidden" &&
          style.display !== "none" &&
          Number(style.opacity) > 0,
        below: below,
        labels: rows.map((row) => (row.textContent || "").replace(/\\s+/g, " ").trim()),
      });
    },
    true
  );
}"""

_DRAIN_JS = """() => {
  const events = window.__gbSelectProbe || [];
  window.__gbSelectProbe = [];
  return events;
}"""

#: Which selects in this document the widget decided to shim.
_SHIM_STATE_JS = """() => {
  const api = window.__guidebot_selects;
  const state = {};
  for (const select of document.querySelectorAll("select")) {
    state[select.id] = !!(api && api.isShimmed(select));
  }
  return state;
}"""

#: The shim lists currently unfurled in this document, and what they offer.
_OPEN_LISTS_JS = """() => {
  const lists = Array.from(document.querySelectorAll("[data-guidebot-select-list]"));
  const open = lists.filter((list) => {
    const rect = list.getBoundingClientRect();
    const style = getComputedStyle(list);
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== "hidden" &&
      style.display !== "none"
    );
  });
  return {
    lists: lists.length,
    open: open.length,
    labels: open.length
      ? Array.from(
          open[0].querySelectorAll("[data-guidebot-option-index]"),
          (row) => (row.textContent || "").replace(/\\s+/g, " ").trim()
        )
      : [],
  };
}"""

_SELECTED_JS = "el => Array.from(el.selectedOptions, (o) => (o.textContent || '').trim())"


async def _quiet(coroutine, fallback):
    """Evaluate against a page that may already be tearing down."""

    try:
        return await coroutine
    except Exception:  # noqa: BLE001 — an observation must never fail the run
        return fallback


class SelectSpy:
    """Observe the shim from inside the browser, while the page is still alive.

    The recorded context is closed before ``run_render`` returns, so nothing can
    be read off the page afterwards. Wrapping :class:`Recorder`'s two entry
    points is what keeps the observation inside the step it belongs to — and, for
    the option click, inside the event itself.
    """

    def __init__(self) -> None:
        #: one entry per ``select:`` step: option, mode, click events, DOM state
        self.selects: list[dict] = []
        #: one entry per ``click:`` step: the shim lists open once it returned
        self.clicks: list[dict] = []

    def reset(self) -> None:
        """Forget the compile pass, so assertions speak about the render."""

        self.selects.clear()
        self.clicks.clear()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self
        original_select = Recorder.select
        original_click = Recorder.click

        async def select(self, target, option, *, native=False):
            await _quiet(self.frame.evaluate(_PROBE_JS), None)
            record = {
                "option": option,
                "native": native,
                "shims": await _quiet(self.frame.evaluate(_SHIM_STATE_JS), {}),
                "events": [],
                "selected": [],
            }
            spy.selects.append(record)
            try:
                await original_select(self, target, option, native=native)
            finally:
                record["events"] = await _quiet(self.frame.evaluate(_DRAIN_JS), [])
                locator = await _quiet(build_locator(self.frame, target), None)
                if locator is not None:
                    record["selected"] = await _quiet(locator.evaluate(_SELECTED_JS), [])

        async def click(self, target, *, before_click=None):
            await original_click(self, target, before_click=before_click)
            spy.clicks.append(await _quiet(self.frame.evaluate(_OPEN_LISTS_JS), {}))

        monkeypatch.setattr(Recorder, "select", select)
        monkeypatch.setattr(Recorder, "click", click)


def _stream_types(path: Path) -> list[str]:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def _write(tmp_path: Path, name: str, template: str, fixture: Path) -> Path:
    path = tmp_path / name
    path.write_text(template.format(url=fixture.resolve().as_uri()), encoding="utf-8")
    return path


def _assert_playable(out: Path) -> None:
    assert out.exists()
    assert probe_duration(out) > 0
    types = _stream_types(out)
    assert types.count("video") == 1
    assert types.count("audio") == 1


# --- the four controls ------------------------------------------------------


async def test_four_controls_compile_and_render_with_the_list_open_at_click_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw, fake-select2, fake-Tom-Select and ``multiple`` — all four survive a
    compile + render, the chosen option ends up selected, and for the three
    drop-downs the list is attached and visible **in the click event itself**.

    All four run on the default ``mode: shim``, with no per-step escape hatch
    anywhere: this is the shipped configuration. The ``<select multiple>`` is
    still never shimmed (spec non-goal — it draws no OS popup), but its rows are
    laid out in the page already, so the cursor clicks the ``<option>`` where it
    sits and the click is asserted to have landed inside the listbox's visible
    box.
    """

    path = _write(tmp_path, "selects.scenario.yaml", FOUR_CONTROLS_SCENARIO, FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        reasoner = SelectReasoner()
        await run_compile_in_browser(path, browser, reasoner)
        assert reasoner.calls == 4

        compiled = load_compiled(compiled_path(path))
        frozen = [action for action in compiled.actions if action is not None]
        assert [action.action for action in frozen] == ["select"] * 4
        # The shim never re-parents a select, so every identity is the <select>'s
        # own — including the two the page enhanced itself.
        assert {action.identity.tag for action in frozen} == {"select"}

        spy.reset()  # from here on, assertions are about the render
        out = tmp_path / "selects.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)

    assert [record["option"] for record in spy.selects] == [
        "BibTeX",
        "Mazowieckie",
        "Lublin",
        "pilne",
    ]
    # Every step ended with exactly the wanted option selected.
    for record in spy.selects:
        assert record["selected"] == [record["option"]]

    # Classification, seen live during the render: only the raw select is shimmed.
    assert spy.selects[0]["shims"] == {
        "format": True,
        "woj": False,
        "miasto": False,
        "tagi": False,
    }

    # The three drop-downs: two beats, and at beat 2 the list was really there.
    for record in spy.selects[:3]:
        kinds = [event["kind"] for event in record["events"]]
        assert kinds == ["control", "option"], record
        opened = record["events"][1]
        assert opened["label"] == record["option"]
        assert opened["attached"] is True
        assert opened["visible"] is True
        assert opened["atBodyLevel"] is True  # nothing can clip it
        assert len(opened["labels"]) == 3
        assert record["option"] in opened["labels"]

    raw, select2, tom = spy.selects[:3]
    # The shim leaves the real <select> as the hit target (its button is
    # pointer-events: none), and its list opens downward.
    assert raw["events"][0] == {"kind": "control", "tag": "select", "id": "format"}
    assert raw["events"][1]["shim"] is True
    assert raw["events"][1]["below"] is True
    assert raw["events"][1]["labels"] == ["lista", "tabela", "BibTeX"]
    # The two enhanced controls are driven through the page's own widget and the
    # page's own list — the shim built neither.
    assert select2["events"][0]["id"] == "woj-widget"
    assert select2["events"][1]["shim"] is False
    assert tom["events"][0]["id"] == "miasto-widget"
    assert tom["events"][1]["shim"] is False

    # <select multiple>: the default mode, no escape hatch, one visible beat —
    # the pointer landed on the <option> itself, inside the listbox's own box.
    listbox = spy.selects[3]
    assert listbox["native"] is False
    assert listbox["events"] == [
        {
            "kind": "listboxOption",
            "label": "pilne",
            "visible": True,
            "insideListbox": True,
        }
    ]
    # ...and it stayed unshimmed throughout: no button, no DOM list of ours.
    assert listbox["shims"]["tagi"] is False


# --- a click: step aimed at a shimmed select --------------------------------


async def test_click_step_on_a_shimmed_select_unfurls_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This path exists because the shim leaves the real ``<select>`` clickable.

    A ``click:`` step therefore lands on the select exactly as before the branch,
    and its ``mousedown`` handler opens the DOM list instead of Chromium's
    unrecordable OS popup.
    """

    path = _write(tmp_path, "click-select.scenario.yaml", CLICK_SCENARIO, FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await run_compile_in_browser(path, browser, SelectReasoner())

        spy.reset()
        out = tmp_path / "click-select.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    assert len(spy.clicks) == 1
    unfurled = spy.clicks[0]
    assert unfurled["open"] == 1
    assert unfurled["labels"] == ["lista", "tabela", "BibTeX"]


# --- a select inside a popup window -----------------------------------------


async def test_select_inside_a_popup_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The init script is context-level, so a popup gets the widget too.

    Worth its own test: in a popup the cursor and the shim list share a single
    document, so the list's z-index has to sit below the cursor's yet above the
    page — in the main window the cursor lives in the shell, above the iframe.
    """

    path = _write(tmp_path, "popup-select.scenario.yaml", POPUP_SCENARIO, POPUP_FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await run_compile_in_browser(path, browser, SelectReasoner())

        compiled = load_compiled(compiled_path(path))
        assert compiled.actions[2] is not None and compiled.actions[2].opens_popup is True

        spy.reset()
        out = tmp_path / "popup-select.mp4"
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    assert len(spy.selects) == 1
    record = spy.selects[0]
    assert record["selected"] == ["Kurier"]
    assert record["shims"] == {"dostawa": True}
    assert [event["kind"] for event in record["events"]] == ["control", "option"]
    opened = record["events"][1]
    assert opened["label"] == "Kurier"
    assert opened["attached"] is True
    assert opened["visible"] is True
    assert opened["labels"] == ["Odbiór osobisty", "Paczkomat", "Kurier"]


# --- back-compatibility -----------------------------------------------------


async def test_artifact_compiled_without_the_shim_still_renders_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most expensive claim on this branch, pinned end to end.

    A ``*.compiled.yaml`` produced *before* the feature must still render *with*
    it, with no recompile. The claim rests on §3: the shim never re-parents the
    ``<select>``, so the frozen ``ancestry_digest`` — a hash of the whole composed
    ancestor chain — stays valid and ``reuse_is_valid`` keeps passing. A wrapper
    design would fail here on every cached action under the select.

    The artifact is built the way a pre-feature compile built one: a bare context
    with no init script at all (``selects=None``), from a scenario with no
    ``selects:`` block, so ``config_hash`` is the pre-feature hash too.
    """

    path = _write(tmp_path, "legacy.scenario.yaml", LEGACY_SCENARIO, FIXTURE)
    url = FIXTURE.resolve().as_uri()
    scenario = load_scenario(path)
    spy = SelectSpy()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        # --- compile exactly as the pre-feature code did: no shim anywhere ---
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await run_compile(path, page, SelectReasoner(), selects=None)
        shimless = await page.evaluate(
            "() => ({api: !!window.__guidebot_selects,"
            " overlays: document.querySelectorAll("
            "'[data-guidebot-select-button],[data-guidebot-select-list]').length})"
        )
        assert shimless == {"api": False, "overlays": 0}
        await context.close()

        artifact = compiled_path(path)
        frozen_bytes = artifact.read_bytes()
        compiled = load_compiled(artifact)
        selects = [action for action in compiled.actions if action is not None]
        assert [action.action for action in selects] == ["select", "select"]
        # The default mode stays out of the projection, so the hash is unchanged.
        assert {action.fingerprint.config_hash for action in selects} == {
            config_hash(scenario.config)
        }

        # --- the identities survive the shim, checked directly ---
        shimmed_context = await browser.new_context(viewport={"width": 800, "height": 600})
        controller = await install_selects(shimmed_context, scenario.config)
        assert controller is not None
        shimmed_page = await shimmed_context.new_page()
        await shimmed_page.goto(url)
        await controller.wait_ready(shimmed_page)
        shim_lists = await shimmed_page.evaluate(
            "() => document.querySelectorAll('[data-guidebot-select-list]').length"
        )
        assert shim_lists == 1  # the raw select really is shimmed on this page
        for action in selects:
            assert await reuse_is_valid(shimmed_page, action), action.identity
        await shimmed_context.close()

        # --- render the pre-feature artifact, with the shim installed ---
        spy.install(monkeypatch)
        out = tmp_path / "legacy.mp4"
        # A regression here surfaces as RenderError "niezgodna tożsamość —
        # uruchom `compile --force`", which is precisely the forced recompile §5
        # promises never to happen.
        await run_render(path, out, FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    _assert_playable(out)
    # Nothing was re-resolved: the sidecar on disk is byte-identical.
    assert compiled_path(path).read_bytes() == frozen_bytes
    assert [record["selected"] for record in spy.selects] == [["BibTeX"], ["Mazowieckie"]]
    # And the pre-feature artifact drove the *new* choreography: a real list.
    for record in spy.selects:
        assert [event["kind"] for event in record["events"]] == ["control", "option"]
        opened = record["events"][1]
        assert opened["visible"] is True
        assert opened["attached"] is True


# --- the undriveable widget -------------------------------------------------


async def _the_select_itself(locator):
    """``user_visible_control`` as it behaved before the predicate was shared.

    Playwright's ``is_visible()`` is true for select2's 1x1-clipped original, so
    the old first step handed the ``<select>`` straight back and validation
    passed. Standing in for it here is what produces the artifact this test
    needs: one an older Guidebot could really have written.
    """

    return await locator.element_handle()


async def test_undriveable_widget_fails_loudly_instead_of_setting_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden ``<select>`` with no visible stand-in must stop the run.

    There is deliberately no fallback to ``select_option()``: it would restore
    exactly the invisible value change the branch exists to remove, and would do
    it unobservably.

    Compile stops it, and *where* it stops is the point. The 1x1-clipped original
    is hidden to the shared predicate, so validation refuses the page and
    ``Recorder.select`` is never reached at all — a step that never runs cannot
    touch the value. Validation used to accept it (Playwright calls a 1x1 box
    visible) and leave the rejecting to the drivability probe one layer down; the
    two compile-time checks disagreeing about the same page is the drift the
    shared predicate removes.

    The second half covers the artifact frozen by an older, more permissive
    validator. Render re-validates a frozen target before reusing it, so this one
    is refused there — the run stops rather than degrading to a silent
    ``select_option``, which is the property that matters. It stops with the
    generic "recompile" verdict of the reuse check rather than the choreography's
    own wording, because it never gets as far as the choreography; recompiling is
    what surfaces the full diagnosis, and the first half is that recompile.
    """

    path = _write(tmp_path, "orphan.scenario.yaml", UNDRIVEABLE_SCENARIO, UNDRIVEABLE_FIXTURE)
    spy = SelectSpy()
    spy.install(monkeypatch)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        with pytest.raises(RuntimeError) as compile_error:
            await run_compile_in_browser(path, browser, SelectReasoner())
        message = str(compile_error.value)
        assert "nie znaleziono widocznej kontrolki" in message
        assert "lista województw" in message  # names the step's own instruction
        # Nothing was ever driven, so nothing could have been quietly set.
        assert spy.selects == []

        # Freeze the artifact the way a Guidebot that still trusted Playwright's
        # `is_visible()` would have frozen it, then render that.
        monkeypatch.setattr(validate_module, "user_visible_control", _the_select_itself)
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        await run_compile(path, page, SelectReasoner(), selects=None)
        await context.close()
        monkeypatch.undo()

        spy.reset()
        spy.install(monkeypatch)
        with pytest.raises(RenderError) as render_error:
            await run_render(path, tmp_path / "orphan.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert "krok 2" in str(render_error.value)
    assert "compile" in str(render_error.value)  # points at the run that diagnoses it
    # Again: refused before anything was driven, so the value was never set.
    assert spy.selects == []
