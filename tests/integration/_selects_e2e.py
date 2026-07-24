"""Shared scaffolding for the select-shim compile → render E2E suite.

A native ``<select>`` draws its option list as an OS popup, so the recording
never shows it. The shim replaces that list with a DOM one; pages that enhanced
their own selects (select2, Tom Select) keep theirs and are driven through it.
The fixture reproduces both *patterns* rather than vendoring the libraries.

What makes this suite worth its runtime is not that the value ends up right —
``select_option`` did that before the branch, invisibly. It is that **at the
moment the option row is clicked the list is attached and visible in the DOM**,
which is asserted from inside the browser by :class:`SelectSpy` rather than
inferred from the final video.

Reasoner mocked (deterministic), TTS fake (silent mp3) — no network, no Codex.

This module is imported explicitly by the ``test_selects_compile_render*.py``
files; it defines the doubles, the in-browser probes and the small filesystem
helpers they share. It carries **no tests and no pytest fixtures** — everything
here is a plain class, constant or function. Two things deliberately do not
travel through an import and so are re-declared in each test file instead:

* the module docstring's *rationale* above (there is no ``conftest.py`` in this
  suite by design, so each file restates why the suite exists), and
* the ``pytestmark`` block — ``pytestmark`` does not propagate through a helper
  import, so each file writes ``pytestmark = list(PYTESTMARK)`` from the
  constant exported here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.models.target import RoleTarget, TestidTarget
from guidebot_recorder.recorder.recorder import Recorder
from guidebot_recorder.resolver.reasoner import ReasonerResult
from guidebot_recorder.resolver.validate import build_locator
from guidebot_recorder.video.mux.probe import probe_duration

#: The marker block every split file installs verbatim as its ``pytestmark``.
#: ``pytestmark`` does not propagate through an import, so a file that merely
#: imported this module would silently lose ``integration``/``ffmpeg`` and the
#: ffmpeg-absent skip — each file must assign ``pytestmark`` from this constant.
PYTESTMARK = [
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


# --- filesystem + playability helpers ---------------------------------------


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
