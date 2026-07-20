"""`render` behaviour for optional branches (`when:` blocks and `optional: true`).

The sidecars are hand-built rather than produced by `compile`: the point of this
phase is what `render` does with a *given* mix of cached and pending entries, and
building them directly is the only way to pin one axis at a time (a data: URL
cannot show a banner on one run and hide it on the next).

Identities are hand-built too. ``capture_identity`` digests only the *(tag, role)*
pairs of the ancestor chain — no sibling index — so every direct child of ``body``
on these fixture pages shares one digest, whatever else is on the page.
"""

import shutil
import textwrap
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from guidebot_recorder.models.action import CachedAction, Fingerprint, PendingAction
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget, TextTarget
from guidebot_recorder.recorder.render import RenderError, run_render
from guidebot_recorder.resolver.identity_capture import _digest_ancestry
from guidebot_recorder.resolver.reasoner import ReasonerError, ReasonerResult
from guidebot_recorder.scenario.compiled import compiled_path, load_compiled, write_compiled
from guidebot_recorder.scenario.loader import load_scenario
from guidebot_recorder.tts.base import Segment
from tests.unit.recorder.test_render import FakeTts

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg niedostępny"),
]

PLAIN_PAGE = "data:text/html,<button>Zaloguj</button>"
BANNER_PAGE = (
    "data:text/html,<div>Ciasteczka</div><button>Akceptuje</button><button>Zaloguj</button>"
)
BODY_CHILD_ANCESTRY = _digest_ancestry([["body", ""], ["html", "document"]])


def _branch_scenario(url: str) -> str:
    return textwrap.dedent(
        f"""\
        config:
          title: Bramka
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{url}"
          - when: "baner cookies"
            timeout: 1
            steps:
              - say: "Akceptujemy ciasteczka."
              - teach: "kliknij Akceptuje"
          - teach: "kliknij Zaloguj"
        """
    )


def _optional_step_scenario(*, optional: bool) -> str:
    marker = "    optional: true\n" if optional else ""
    return textwrap.dedent(
        f"""\
        config:
          title: Krok opcjonalny
          viewport: {{width: 640, height: 480}}
          tts: {{provider: fake, voice: v, lang: pl-PL}}
        steps:
          - navigate: "{PLAIN_PAGE}"
          - teach: "kliknij Akceptuje"
        """
    ) + marker + '  - teach: "kliknij Zaloguj"\n'


def _identity(tag: str) -> Identity:
    return Identity(tag=tag, ancestry_digest=BODY_CHILD_ANCESTRY)


def _fingerprint(chash: str, *, kind: str, source: str, state: str | None) -> Fingerprint:
    return Fingerprint(
        command_kind=kind,
        compiled_from=source,
        expect="none",
        config_hash=chash,
        state=state,
    )


def _gate(chash: str) -> CachedAction:
    return CachedAction(
        action="waitFor",
        target=TextTarget(text="Ciasteczka", exact=False),
        identity=_identity("div"),
        expect="none",
        state="visible",
        fingerprint=_fingerprint(chash, kind="wait", source="baner cookies", state="visible"),
    )


def _click(chash: str, *, name: str, source: str) -> CachedAction:
    return CachedAction(
        action="click",
        target=RoleTarget(role="button", name=name, exact=True),
        identity=_identity("button"),
        expect="none",
        fingerprint=_fingerprint(chash, kind="teach", source=source, state=None),
    )


def _pending(chash: str, *, kind: str, source: str, state: str | None = None) -> PendingAction:
    return PendingAction(fingerprint=_fingerprint(chash, kind=kind, source=source, state=state))


def _write(path: Path, actions: list) -> None:
    write_compiled(compiled_path(path), CompiledScenario(source=path.name, actions=actions))


def _chash(path: Path) -> str:
    return config_hash(load_scenario(path).config)


def _branch_sidecar(path: Path, *, gate_pending: bool, child_pending: bool) -> None:
    chash = _chash(path)
    _write(
        path,
        [
            None,
            _pending(chash, kind="wait", source="baner cookies", state="visible")
            if gate_pending
            else _gate(chash),
            None,
            _pending(chash, kind="teach", source="kliknij Akceptuje")
            if child_pending
            else _click(chash, name="Akceptuje", source="kliknij Akceptuje"),
            _click(chash, name="Zaloguj", source="kliknij Zaloguj"),
        ],
    )


@pytest.fixture
def narration_spy(monkeypatch):
    """Record every step whose narration actually reached the timeline."""

    waited: list[float] = []

    async def observe(segments: list[Segment], **kwargs) -> int | None:
        if segments:
            waited.append(max(segment.duration for segment in segments))
        # Pacing itself is stubbed out (these tests care only about *which*
        # steps narrate, not how long they hold), so no freeze is emitted.
        return None

    monkeypatch.setattr("guidebot_recorder.recorder.render._pace_narration", observe)
    return waited


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        launched = await pw.chromium.launch(headless=True)
        yield launched
        await launched.close()


class GateReasoner:
    """Resolves the banner and its button; absent for the first ``absent_calls`` tries."""

    def __init__(self, absent_calls: int = 0, reason: str = "no_handle") -> None:
        self.calls: list[str] = []
        self.absent_calls = absent_calls
        self.reason = reason

    async def resolve(self, instruction, candidates):
        self.calls.append(instruction)
        if len(self.calls) <= self.absent_calls:
            return ReasonerError(reason=self.reason, message="nie widzę takiego elementu")
        if "baner" in instruction:
            return ReasonerResult(
                action="waitFor", target=TextTarget(text="Ciasteczka", exact=False)
            )
        name = "Akceptuje" if "Akceptuje" in instruction else "Zaloguj"
        return ReasonerResult(
            action="click", target=RoleTarget(role="button", name=name, exact=True)
        )


class AlwaysAbsentReasoner:
    def __init__(self, reason: str = "no_handle") -> None:
        self.calls: list[str] = []
        self.reason = reason

    async def resolve(self, instruction, candidates):
        self.calls.append(instruction)
        return ReasonerError(reason=self.reason, message="nie ma")


# --- cached gate that times out ----------------------------------------------------


async def test_gate_timeout_skips_branch_and_children_but_not_later_steps(
    tmp_path, browser, narration_spy
):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(PLAIN_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=False, child_pending=False)

    out = tmp_path / "out.mp4"
    await run_render(path, out, FakeTts(), tmp_path / "cache", browser)

    assert out.exists()
    # only the trailing `teach` narrated; the branch's say + teach were dropped
    assert len(narration_spy) == 1


async def test_skipped_branch_drops_its_narration_from_the_audio_bed(
    tmp_path, browser, monkeypatch
):
    import guidebot_recorder.recorder.render as render_module

    original = render_module.build_audio_bed
    placed: list[int] = []

    def spy(placements, total, out):
        placed.append(len(placements))
        return original(placements, total, out)

    monkeypatch.setattr(render_module, "build_audio_bed", spy)

    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(PLAIN_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=False, child_pending=False)

    await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)

    assert placed == [1]


# --- pending gate resolved in place ------------------------------------------------


async def test_pending_gate_polls_resolves_executes_children_and_rewrites_sidecar(
    tmp_path, browser
):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(BANNER_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=True, child_pending=True)
    # the first probe misses: the canonical banner shows up after a delay, so a
    # single snapshot would produce a spurious skip
    reasoner = GateReasoner(absent_calls=1)

    await run_render(
        path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser, reasoner=reasoner
    )

    assert reasoner.calls.count("baner cookies") >= 2  # polled rather than snapshotted
    compiled = load_compiled(compiled_path(path))
    assert isinstance(compiled.actions[1], CachedAction)
    assert compiled.actions[1].action == "waitFor"
    assert isinstance(compiled.actions[3], CachedAction)
    assert compiled.actions[3].action == "click"
    # the entry after the branch is untouched by the rewrite
    assert isinstance(compiled.actions[4], CachedAction)


async def test_pending_gate_that_never_appears_skips_the_branch(tmp_path, browser, narration_spy):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(PLAIN_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=True, child_pending=True)

    await run_render(
        path,
        tmp_path / "out.mp4",
        FakeTts(),
        tmp_path / "cache",
        browser,
        reasoner=AlwaysAbsentReasoner(),
    )

    assert len(narration_spy) == 1
    compiled = load_compiled(compiled_path(path))
    assert isinstance(compiled.actions[1], PendingAction)  # still pending


# --- reasoner unavailable -----------------------------------------------------------


async def test_missing_reasoner_warns_and_skips_instead_of_failing(
    tmp_path, browser, capsys, narration_spy
):
    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(BANNER_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=True, child_pending=True)

    await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)

    assert "reasoner" in capsys.readouterr().out.lower()
    assert len(narration_spy) == 1


# --- the error boundary --------------------------------------------------------------


async def test_multiple_actions_still_fails_the_render(tmp_path, browser):
    """An ambiguous description is an authoring error, not an absent element."""

    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(BANNER_PAGE), encoding="utf-8")
    _branch_sidecar(path, gate_pending=True, child_pending=True)

    with pytest.raises(RenderError, match="multiple_actions"):
        await run_render(
            path,
            tmp_path / "out.mp4",
            FakeTts(),
            tmp_path / "cache",
            browser,
            reasoner=AlwaysAbsentReasoner(reason="multiple_actions"),
        )


async def test_error_inside_an_entered_branch_still_fails_the_render(tmp_path, browser):
    """The gate is there; a child whose frozen target is gone is a real regression."""

    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario("data:text/html,<div>Ciasteczka</div>"), encoding="utf-8")
    _branch_sidecar(path, gate_pending=False, child_pending=False)

    with pytest.raises(RenderError, match="krok 3"):
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)


# --- `optional: true` on a single step ------------------------------------------------


async def test_optional_step_with_a_stale_cached_target_is_skipped(
    tmp_path, browser, narration_spy
):
    path = tmp_path / "opt.scenario.yaml"
    path.write_text(_optional_step_scenario(optional=True), encoding="utf-8")
    chash = _chash(path)
    _write(
        path,
        [
            None,
            _click(chash, name="Akceptuje", source="kliknij Akceptuje"),
            _click(chash, name="Zaloguj", source="kliknij Zaloguj"),
        ],
    )

    await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)

    assert len(narration_spy) == 1  # the optional step's narration was dropped too


async def test_required_step_with_a_stale_cached_target_still_fails(tmp_path, browser):
    path = tmp_path / "req.scenario.yaml"
    path.write_text(_optional_step_scenario(optional=False), encoding="utf-8")
    chash = _chash(path)
    _write(
        path,
        [
            None,
            _click(chash, name="Akceptuje", source="kliknij Akceptuje"),
            _click(chash, name="Zaloguj", source="kliknij Zaloguj"),
        ],
    )

    with pytest.raises(RenderError, match="niezgodna tożsamość"):
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)


# --- flat indexing ---------------------------------------------------------------------


async def test_step_numbering_uses_flat_indices(tmp_path, browser):
    """A stale entry inside a branch is reported at its flat index, not its block's."""

    path = tmp_path / "gate.scenario.yaml"
    path.write_text(_branch_scenario(PLAIN_PAGE), encoding="utf-8")
    chash = _chash(path)
    _write(
        path,
        [
            None,
            _gate(chash),
            None,
            _click(chash, name="Akceptuje", source="zła instrukcja"),
            _click(chash, name="Zaloguj", source="kliknij Zaloguj"),
        ],
    )

    with pytest.raises(RenderError, match="krok 3"):
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)


async def test_pending_entry_on_a_required_step_demands_a_recompile(tmp_path, browser):
    path = tmp_path / "req.scenario.yaml"
    path.write_text(_optional_step_scenario(optional=False), encoding="utf-8")
    chash = _chash(path)
    _write(
        path,
        [
            None,
            _pending(chash, kind="teach", source="kliknij Akceptuje"),
            _click(chash, name="Zaloguj", source="kliknij Zaloguj"),
        ],
    )

    with pytest.raises(RenderError, match="krok 1"):
        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
