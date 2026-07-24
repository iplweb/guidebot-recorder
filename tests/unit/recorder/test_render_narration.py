"""``recorder.render.narration`` pacing and the hold-frame film.

Split out of the original ``test_render.py``. The pure freeze-merge unit tests
for ``_build_timeline`` live in ``test_render_timeline.py``.
"""

import textwrap
import time

import pytest
from playwright.async_api import async_playwright
from pydantic import ValidationError

from guidebot_recorder.models.config import MIN_HOLD_FRAME_SETTLE
from guidebot_recorder.recorder.compile import run_compile
from guidebot_recorder.recorder.render import run_render
from guidebot_recorder.recorder.render.narration import _pace_narration
from guidebot_recorder.scenario.loader import ScenarioValidationError, load_scenario
from guidebot_recorder.video.audiobed import Placed
from guidebot_recorder.video.mux.probe import probe_duration
from guidebot_recorder.video.timeline import TimeEdit, Timeline, probe_frame_count

from ._render_helpers import FFMPEG, FakeTts, LongTts, MockReasoner, TwoSecondTts

pytestmark = FFMPEG


class _Seg:
    def __init__(self, duration: float) -> None:
        self.duration = duration


async def test_pace_narration_sleeps_in_full_when_disabled() -> None:
    edits: list[TimeEdit] = []
    started = time.monotonic()
    await _pace_narration([_Seg(0.3)], anchor=started, hold_frame=False, settle=0.1, edits=edits)
    assert time.monotonic() - started >= 0.3
    assert edits == []


async def test_pace_narration_records_a_freeze_for_the_remainder() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration([_Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits)
    elapsed = time.monotonic() - anchor
    # Only the settle is paid in real time.
    assert elapsed < 1.0
    assert len(edits) == 1
    assert edits[0].kind == "freeze"
    # 2.0s narration - 0.1s settle = 1.9s -> 48 frames (rounded to the grid)
    assert edits[0].frames == 48


async def test_pace_narration_uses_the_longest_language() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration(
        [_Seg(0.5), _Seg(2.0)], anchor=anchor, hold_frame=True, settle=0.1, edits=edits
    )
    assert edits[0].frames == 48


async def test_pace_narration_emits_no_freeze_when_narration_is_shorter_than_settle() -> None:
    edits: list[TimeEdit] = []
    anchor = time.monotonic()
    await _pace_narration([_Seg(0.2)], anchor=anchor, hold_frame=True, settle=1.0, edits=edits)
    assert time.monotonic() - anchor >= 0.2
    assert edits == []


async def test_pace_narration_ignores_empty_segments() -> None:
    edits: list[TimeEdit] = []
    await _pace_narration([], anchor=time.monotonic(), hold_frame=True, settle=1.0, edits=edits)
    assert edits == []


async def test_run_render_hold_frame_overrides_reach_the_pacing_decision(tmp_path, monkeypatch):
    # The CLI passes its flags as keyword overrides because `run_render` loads
    # the scenario itself — mutating a caller-side Config would be a no-op. This
    # asserts the override really lands on the pacing call, not just on `cfg`.
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0.2
            steps:
              - say: "Krok pierwszy."
            """
        ),
        encoding="utf-8",
    )

    pacing_kwargs: list[dict] = []
    original = R.narration._pace_narration

    async def spy(segments, **kwargs):
        pacing_kwargs.append(kwargs)
        await original(segments, **kwargs)

    monkeypatch.setattr(R.narration, "_pace_narration", spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        await run_render(
            path,
            tmp_path / "out.mp4",
            FakeTts(),
            tmp_path / "cache",
            browser,
            hold_frame=False,
            hold_frame_settle=0.75,
        )
        await browser.close()

    assert pacing_kwargs, "narration pacing never ran"
    assert pacing_kwargs[0]["hold_frame"] is False, "the --no-hold-frame override was discarded"
    assert pacing_kwargs[0]["settle"] == 0.75, "the --hold-frame-settle override was discarded"


async def test_run_render_uses_the_scenario_value_when_no_override_is_given(tmp_path, monkeypatch):
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold-default.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: false
              holdFrameSettle: 0.25
            steps:
              - say: "Krok pierwszy."
            """
        ),
        encoding="utf-8",
    )

    pacing_kwargs: list[dict] = []
    original = R.narration._pace_narration

    async def spy(segments, **kwargs):
        pacing_kwargs.append(kwargs)
        await original(segments, **kwargs)

    monkeypatch.setattr(R.narration, "_pace_narration", spy)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()

        await run_render(path, tmp_path / "out.mp4", FakeTts(), tmp_path / "cache", browser)
        await browser.close()

    assert pacing_kwargs[0]["hold_frame"] is False
    assert pacing_kwargs[0]["settle"] == 0.25


async def test_hold_frame_film_matches_the_model_exactly(tmp_path, monkeypatch):
    """The finished film is exactly as long as the time model says it is.

    This is the deterministic form of "hold-frame preserves the pacing": the
    earlier version rendered the scenario twice and compared the two durations
    within a tolerance, but the no-hold baseline is pure wall clock and drifted
    run to run — the tolerance was absorbing that jitter rather than proving
    anything. Frame counts on both sides are integers, so they can be compared
    for equality.
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "hold.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zamrożona klatka
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0.4
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
            """
        ),
        encoding="utf-8",
    )

    seen: list[Timeline] = []
    original = R.timeline._apply_timeline_edits

    def spy(source, timeline, dest):
        seen.append(timeline)
        return original(source, timeline, dest)

    monkeypatch.setattr(R.timeline, "_apply_timeline_edits", spy)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()
        await run_render(path, out, LongTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen, "two 3.0s narrations under a 0.4s settle emitted no freezes"
    timeline = seen[0]
    # The point of the feature, stated deterministically: the recording the
    # browser produced is shorter than the film that ships.
    assert timeline.source_frames < timeline.virtual_frames
    # ...and the file on disk is exactly what the model promised.
    assert probe_frame_count(out) == timeline.virtual_frames

    # The audio beds are built from the model's duration, so they must line up
    # with the file the model produced — coverage the unedited path cannot give.
    beds = list((tmp_path / ".guidebot_video" / "out").glob("bed-*.wav"))
    assert beds, "no narration bed was published"
    for bed in beds:
        assert probe_duration(bed) == pytest.approx(probe_duration(out), abs=0.05)


def test_zero_settle_is_rejected_at_scenario_load(tmp_path) -> None:
    """`holdFrameSettle: 0` is sub-frame and must never reach the recorder.

    It used to be a legal config value: the pacing loop stamped several steps
    onto the same 25fps frame, and the strict `Timeline` rejected that — but
    only after the whole recording had already completed. Rejecting it at
    config validation moves that failure to before any recording happens.

    Note what this floor does NOT do, despite an earlier claim here: it does
    not stop narration offsets from collapsing onto each other. Settle bounds
    the distance from a step's start to its own freeze, not the distance from
    that freeze to the NEXT step's stamp, which is where the collapse actually
    occurred — and it occurred at the DEFAULT settle too. The guard against
    that is monotonic stamping (`_stamp_frame`), asserted by
    `test_hold_frame_narrations_never_overlap`. This floor stands on its own
    footing: a sub-frame settle is not representable on the frame grid.
    """
    path = tmp_path / "zero-settle.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Zero
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
              holdFrameSettle: 0
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
              - say: "Trzeci."
              - say: "Czwarty."
              - say: "Piąty."
            """
        ),
        encoding="utf-8",
    )

    # `load_scenario` tłumaczy błędy pydantica na banner `plik:linia`, więc na
    # zewnątrz wychodzi `ScenarioValidationError` (dalej `ValueError`)
    with pytest.raises(ScenarioValidationError, match="holdFrameSettle"):
        load_scenario(path)


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_hold_frame_settle_override_is_validated(value) -> None:
    """The `--hold-frame-settle` override obeys the same floor as the config field.

    `run_render` applies the CLI overrides by ASSIGNING onto the loaded
    `Config`. Pydantic skips field constraints on assignment unless the model
    opts in, so this path used to accept anything: `0` reproduced the very
    sub-frame settle the field rejects, and a negative value made the held
    frame LONGER than the narration (`remaining = duration - settle`), quietly
    inflating the film past its own audio. `validate_assignment` closes it for
    every field at once.
    """
    from guidebot_recorder.models.config import Config

    cfg = Config(
        title="T",
        viewport={"width": 640, "height": 480},
        tts={"provider": "fake", "voice": "v", "lang": "pl-PL"},
    )
    with pytest.raises(ValidationError):
        cfg.hold_frame_settle = value
    assert cfg.hold_frame_settle == 1.0


async def test_smallest_legal_settle_still_renders(tmp_path, monkeypatch):
    """`Config`'s smallest legal `holdFrameSettle` still renders end-to-end.

    This replaces the old settle=0 render test now that 0 is rejected at
    config validation (see `test_zero_settle_is_rejected_at_scenario_load`).
    It proves the render pipeline still holds a still frame and produces a
    file matching the model at the *smallest value `Config` actually accepts*
    — the merge/clamp logic in `_build_timeline` itself stays covered
    independently of this test, by the pure `_build_timeline` unit tests
    below (they build a `Timeline` directly and need no legal `Config` at
    all).
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "min-settle.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: MinSettle
              viewport: {{width: 640, height: 480}}
              tts: {{provider: fake, voice: v, lang: pl-PL}}
              holdFrameForNarration: true
              holdFrameSettle: {MIN_HOLD_FRAME_SETTLE}
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
              - say: "Trzeci."
              - say: "Czwarty."
              - say: "Piaty."
            """
        ),
        encoding="utf-8",
    )

    seen: list[Timeline] = []
    original = R.timeline._apply_timeline_edits

    def spy(source, timeline, dest):
        seen.append(timeline)
        return original(source, timeline, dest)

    monkeypatch.setattr(R.timeline, "_apply_timeline_edits", spy)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()
        await run_render(path, out, LongTts(), tmp_path / "cache", browser)
        await browser.close()

    assert seen, "no freezes were recorded at the minimum legal settle"
    timeline = seen[0]
    # Five 3.0s narrations, almost none of it paid in real time: the finished
    # film is well past what the browser actually recorded, and the file on
    # disk is exactly what the model promised — the same shape of assertion
    # `test_hold_frame_film_matches_the_model_exactly` makes, at the opposite
    # (minimum legal) end of the settle range.
    assert timeline.source_frames < timeline.virtual_frames
    assert probe_frame_count(out) == timeline.virtual_frames
    # Five steps at a two-frame settle land only a frame or two apart, which is
    # the shape that used to lose a frame in the concat stage — see
    # `test_closely_spaced_freezes_stay_frame_exact`. Two steps did not reach it.
    assert len(timeline.edits) >= 3


async def test_hold_frame_narrations_never_overlap(tmp_path, monkeypatch):
    """Consecutive narrations are PLACED in sequence, not merely summed to length.

    Every other hold-frame guard compares LENGTHS — `probe_frame_count ==
    virtual_frames`, the mux duration tolerance, the narration-overrun check.
    All of them pass while narration offsets silently collapse onto each other,
    because a collapsed offset does not change how long the film is.

    This asserts PLACEMENT: with eight consecutive `say` steps at the DEFAULT
    settle, each narration must start no earlier than the end of the one before
    it. It fails when a step's raw wall-clock stamp rounds onto the same 25fps
    frame as the previous step's freeze, since `Timeline.to_virtual` shifts a
    stamp only when a freeze sits STRICTLY before it — so such a stamp maps to
    the START of the hold and plays on top of the previous voice-over.
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "no-overlap.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Bez nakladek
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              holdFrameForNarration: true
            steps:
              - say: "Pierwszy."
              - say: "Drugi."
              - say: "Trzeci."
              - say: "Czwarty."
              - say: "Piaty."
              - say: "Szosty."
              - say: "Siodmy."
              - say: "Osmy."
            """
        ),
        encoding="utf-8",
    )

    # The offsets actually handed to the audio bed, captured at the boundary
    # where the frame axis becomes seconds.
    captured: list[list[Placed]] = []
    original = R.audio._assemble_audio_tracks

    async def spy(video, configs, placed_by_language, total, *args, **kwargs):
        captured.extend(placed_by_language.values())
        return await original(video, configs, placed_by_language, total, *args, **kwargs)

    monkeypatch.setattr(R.audio, "_assemble_audio_tracks", spy)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()
        await run_render(path, out, TwoSecondTts(), tmp_path / "cache", browser)
        await browser.close()

    assert captured, "no narration was placed"
    for placed in captured:
        offsets = [round(p.offset, 3) for p in placed]
        gaps = [round(b - a, 3) for a, b in zip(offsets, offsets[1:], strict=False)]
        for index, (gap, previous) in enumerate(zip(gaps, placed, strict=False)):
            assert gap >= previous.segment.duration - 1e-6, (
                f"narration {index + 1} starts {previous.segment.duration - gap:.2f}s "
                f"before narration {index} ends — offsets={offsets} gaps={gaps}"
            )


async def test_sfx_after_a_freeze_never_lands_inside_the_hold(tmp_path, monkeypatch):
    """A sound effect stamped right after a freeze must land AFTER it, not inside it.

    `test_hold_frame_narrations_never_overlap` proves the narration clamp
    (`not_before=narration_frame` on the NEXT step's own narration stamp); it
    never looks at SFX. `_Clock.note_sfx` (render/clock.py, the bound method
    handed to `Recorder(on_sfx=...)`) carries the exact same
    `not_before=last_freeze_frame + 1` clamp, but nothing asserts it does anything — the render
    could clamp narration and NOT sound effects and the whole suite would
    stay green, since `test_render_with_sound_collects_and_mixes_sfx` only
    checks the events list is non-empty, never an offset.

    In an ordinary scenario this rarely matters: a click's cursor glide
    (`cursor.min_duration`, default 320ms) and its settle pause (`cursor.
    settle`, default 280ms) put well over a frame of real time between a
    freeze and the click that follows it, so the raw wall-clock stamp is
    already past the freeze without any help from the clamp. This scenario
    removes that margin on purpose: the first `click` parks the cursor
    exactly on the button, so the second `click` — the one carried by the
    narrated step, right after its freeze — glides ZERO pixels. With
    `minDuration: 0` that is an instant, zero-duration move, leaving only a
    couple of CDP round-trips between the freeze being stamped and the click's
    `on_sfx("click")` firing — the same margin `_stamp_frame`'s own docstring
    says a raw stamp can lose to.

    Without the clamp, that click's raw stamp can land ON the freeze's own
    frame, which `Timeline.to_virtual` maps to the START of the hold — right
    where the narration begins, roughly `hold_frame_settle` seconds into a
    2-second narration, not at its end. With the clamp it lands one frame
    past the freeze, which `to_virtual` maps at or after the END of the hold,
    i.e. at or after the end of the narration it was clamped against.
    """
    import guidebot_recorder.recorder.render as R

    path = tmp_path / "sfx-clamp.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: SFX kontra zamrozenie
              viewport: {width: 640, height: 480}
              tts: {provider: fake, voice: v, lang: pl-PL}
              sound: {enabled: true, click: true, keys: true, volume: -12}
              cursor: {minDuration: 0, settle: 0}
              holdFrameForNarration: true
            steps:
              - navigate: "data:text/html,<button>Zaloguj</button>"
              - click: "Zaloguj"
              - click: "Zaloguj"
                say: "Kliknij ponownie, aby przejsc dalej."
            """
        ),
        encoding="utf-8",
    )

    narrations: list[list[Placed]] = []
    original_assemble = R.audio._assemble_audio_tracks

    async def spy_assemble(video, configs, placed_by_language, total, *args, **kwargs):
        narrations.extend(placed_by_language.values())
        return await original_assemble(video, configs, placed_by_language, total, *args, **kwargs)

    monkeypatch.setattr(R.audio, "_assemble_audio_tracks", spy_assemble)

    sfx_events: list[list[tuple[str, float]]] = []
    original_build_sfx_bed = R.audio.build_sfx_bed

    def spy_build_sfx_bed(events, *args, **kwargs):
        sfx_events.append(list(events))
        return original_build_sfx_bed(events, *args, **kwargs)

    monkeypatch.setattr(R.audio, "build_sfx_bed", spy_build_sfx_bed)

    out = tmp_path / "out.mp4"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await run_compile(path, page, MockReasoner(), selects=None)
        await page.context.close()
        await run_render(path, out, TwoSecondTts(), tmp_path / "cache", browser)
        await browser.close()

    assert sfx_events, "build_sfx_bed was never called"
    clicks = [offset for kind, offset in sfx_events[0] if kind == "click"]
    assert len(clicks) == 2, f"expected two clicks, got {clicks}"
    # clicks[0] is the first (unnarrated) click; clicks[1] is the one carried
    # by the narrated step, stamped right after that step's freeze.
    narrated_click = clicks[1]

    placed = [p for lang_placed in narrations for p in lang_placed]
    assert placed, "no narration was placed"
    narration = placed[0]
    narration_end = narration.offset + narration.segment.duration

    assert narrated_click >= narration_end - 1e-6, (
        f"click landed {narration_end - narrated_click:.3f}s inside the hold "
        f"(click={narrated_click}, narration ends at {narration_end})"
    )
