import pytest

from guidebot_recorder.guide.prolog import GuideError, classify, scan_for_blockers
from guidebot_recorder.models.action import CachedAction, Fingerprint, PendingAction
from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import FlatStep, Scenario, Step, WhenBlock
from guidebot_recorder.models.target import RoleTarget


def _cfg():
    return Config(
        title="t",
        viewport=Viewport(width=1280, height=720),
        tts=TtsConfig(provider="p", voice="v", lang="eng"),
        base_url="https://example.com",
    )


def _fp(command_kind="click"):
    return Fingerprint(command_kind=command_kind, compiled_from="x", expect="none", config_hash="c")


def _cached(action="click", opens_popup=False):
    return CachedAction(
        action=action,
        target=RoleTarget(role="button", name="x"),
        expect="none",
        opens_popup=opens_popup,
        fingerprint=_fp(),
    )


def _pending():
    return PendingAction(fingerprint=_fp())


def classify_step_of(step):
    return classify(FlatStep(step=step, branch=None, is_gate=False))


def test_classify_kinds():
    assert classify_step_of(Step(navigate="https://x")) == "navigate"
    assert classify_step_of(Step(slide={"title": "Sekcja"})) == "slide"
    assert classify_step_of(Step(click="btn")) == "action"
    assert classify_step_of(Step(say="tylko narracja")) == "text"
    assert classify_step_of(Step(wait=1.5)) == "wait"
    assert classify_step_of(Step(wait=1.5, say="czekamy")) == "text"


def test_classify_select_is_an_action():
    assert classify_step_of(Step(select={"from": "zakres", "option": "Zakres lat"})) == "action"


def test_classify_scroll_is_its_own_kind_regardless_of_say():
    assert classify_step_of(Step(scroll="down")) == "scroll"
    assert classify_step_of(Step(scroll="down", say="Przewijamy w dół")) == "scroll"


def test_scan_rejects_a_command_the_guide_cannot_replay(monkeypatch):
    """Komenda spoza `SUPPORTED_KINDS` pada w preflighcie, a nie po cichu.

    Tą cichą ścieżką przeszedł brak `select`: `classify` mapował nieznaną
    komendę na stronę tekstową, przeglądarka nigdy nie dostawała akcji, a błąd
    wychodził dopiero kilka kroków dalej — jako niezgodna tożsamość na stronie,
    której kompilator nigdy nie widział. Symulujemy przyszłą komendę, bo dziś
    każda istniejąca jest już obsłużona.
    """

    scen = Scenario(config=_cfg(), steps=[Step(say="cokolwiek")])
    monkeypatch.setattr(Step, "command_kind", lambda self: "teleport")
    with pytest.raises(GuideError, match="teleport"):
        scan_for_blockers(scen.flat_steps(), [None])


def test_scan_allows_a_visual_only_command():
    """`desktop` to ozdobnik filmu (jak `slide`) — w PDF-ie zostaje narracja."""

    scen = Scenario(config=_cfg(), steps=[Step(desktop={"icon": "chrome"}, say="otwieram")])
    scan_for_blockers(scen.flat_steps(), [None])  # no raise


def test_scan_raises_on_popup():
    scen = Scenario(config=_cfg(), steps=[Step(click="opens something")])
    with pytest.raises(GuideError, match="popup"):
        scan_for_blockers(scen.flat_steps(), [_cached(opens_popup=True)])


def test_scan_raises_on_mandatory_pending():
    scen = Scenario(config=_cfg(), steps=[Step(click="btn")])
    with pytest.raises(GuideError, match="compile"):
        scan_for_blockers(scen.flat_steps(), [_pending()])


def test_scan_allows_pending_on_optional():
    scen = Scenario(config=_cfg(), steps=[Step(click="btn", optional=True)])
    scan_for_blockers(scen.flat_steps(), [_pending()])  # no raise


def test_scan_allows_pending_on_gate():
    scen = Scenario(config=_cfg(), steps=[WhenBlock(when="a banner", steps=[Step(click="ok")])])
    # gate action pending + child cached is fine (branch may never have compiled)
    scan_for_blockers(scen.flat_steps(), [_pending(), _cached()])
