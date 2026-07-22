"""Bloki „tylko do renderu": kursor, pisanie, dźwięk, intro, hold-frame, pulpit, fade.

To, co łączy te bloki, jest jednym zdaniem: **żaden z nich nie może zmienić
`config_hash()`**. Kompilacja scenariusza ich nie widzi, więc przekręcenie
suwaka nie ma prawa unieważnić skompilowanego sidecara. Dlatego siedzą razem,
mimo że dotyczą różnych podsystemów — testy regresji hasha są tu połową
zawartości pliku.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    MIN_HOLD_FRAME_SETTLE,
    Config,
    DesktopConfig,
    FadeConfig,
    TtsConfig,
    Viewport,
    config_hash,
)

from ._config_helpers import _cfg


# Task 0.1: CursorClick tests
def test_cursor_click_defaults_match_todays_ripple():
    from guidebot_recorder.models.config import CursorClick

    c = CursorClick()
    assert c.color == "rgba(37,99,235,.9)"
    assert c.scale == 3.25
    assert c.flash is False


def test_cursor_click_rejects_unknown_keys_and_nonpositive_scale():
    from guidebot_recorder.models.config import CursorClick

    with pytest.raises(ValidationError):
        CursorClick(bogus=1)
    with pytest.raises(ValidationError):
        CursorClick(scale=0)


def test_cursor_config_has_click_field_with_defaults():
    from guidebot_recorder.models.config import CursorConfig

    c = CursorConfig()
    assert c.click.color == "rgba(37,99,235,.9)"
    assert c.click.scale == 3.25
    assert c.click.flash is False


# Task 0.2: TypingConfig tests
def test_typing_config_defaults_and_bounds():
    from guidebot_recorder.models.config import TypingConfig

    t = TypingConfig()
    assert t.animate is True and t.speed == 60 and t.jitter_ms == 40
    assert TypingConfig(jitterMs=25).jitter_ms == 25  # camelCase alias
    with pytest.raises(ValidationError):
        TypingConfig(speed=0)
    with pytest.raises(ValidationError):
        TypingConfig(jitter_ms=-1)  # ge=0
    with pytest.raises(ValidationError):
        TypingConfig(bogus=1)


# Task 0.3: SoundConfig tests
def test_sound_config_defaults_and_bounds():
    from guidebot_recorder.models.config import SoundConfig

    s = SoundConfig()
    assert (s.enabled, s.click, s.keys, s.volume) == (True, True, True, -12.0)
    with pytest.raises(ValidationError):
        SoundConfig(volume=3.0)  # positive gain rejected (le=0)
    with pytest.raises(ValidationError):
        SoundConfig(bogus=1)


# Task 0.4: IntroConfig tests
def test_intro_config_defaults():
    from guidebot_recorder.models.config import IntroConfig

    i = IntroConfig()
    assert i.enabled is False and i.subtitle is None and i.notes is None


# Task 0.5: config_hash() regression test
def test_new_render_only_blocks_do_not_change_config_hash():
    from guidebot_recorder.models.config import (
        Config,
        CursorClick,
        CursorConfig,
        IntroConfig,
        SoundConfig,
        TtsConfig,
        TypingConfig,
        Viewport,
    )

    base = Config(
        title="t",
        viewport=Viewport(width=800, height=600),
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
    )
    h0 = config_hash(base)
    mutated = base.model_copy(
        update={
            "cursor": CursorConfig(click=CursorClick(flash=True, scale=4.5)),
            "typing": TypingConfig(animate=True, speed=40),
            "sound": SoundConfig(enabled=True, volume=-6.0),
            "intro": IntroConfig(enabled=True, subtitle="s"),
        }
    )
    assert config_hash(mutated) == h0


def test_hold_frame_defaults_to_on() -> None:
    cfg = Config(
        title="t",
        viewport=Viewport(width=1280, height=720),
        tts=TtsConfig(provider="edge", lang="pl", voice="pl-PL-ZofiaNeural"),
    )
    assert cfg.hold_frame_for_narration is True
    assert cfg.hold_frame_settle == 1.0


def test_hold_frame_accepts_camel_case_aliases() -> None:
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "lang": "pl", "voice": "pl-PL-ZofiaNeural"},
            "holdFrameForNarration": False,
            "holdFrameSettle": 0.5,
        }
    )
    assert cfg.hold_frame_for_narration is False
    assert cfg.hold_frame_settle == 0.5


@pytest.mark.parametrize(
    "settle",
    [
        -1.0,
        0.0,
        MIN_HOLD_FRAME_SETTLE / 2,  # sub-frame: half the two-frame floor
    ],
)
def test_hold_frame_settle_rejects_below_the_two_frame_floor(settle: float) -> None:
    """Both negative and sub-frame settle values must be rejected at load time.

    Negative values are nonsensical; sub-frame values are not representable on
    the renderer's 25fps grid at all (see `MIN_HOLD_FRAME_SETTLE`'s comment).

    This is NOT what stops narration from colliding with the previous step's
    freeze — that guard is monotonic stamping in `recorder.render._stamp_frame`,
    which applies at every settle value, including this floor. Settle only
    separates a step's own start from its own freeze; the distance from that
    freeze to the NEXT step's stamp is unrelated to it.
    """
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"provider": "edge", "lang": "pl", "voice": "pl-PL-ZofiaNeural"},
                "holdFrameSettle": settle,
            }
        )


def test_hold_frame_settle_accepts_the_two_frame_floor() -> None:
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "lang": "pl", "voice": "pl-PL-ZofiaNeural"},
            "holdFrameSettle": MIN_HOLD_FRAME_SETTLE,
        }
    )
    assert cfg.hold_frame_settle == MIN_HOLD_FRAME_SETTLE


def test_hold_frame_is_not_part_of_config_hash() -> None:
    """Render-only pacing must never invalidate compiled references."""
    base = {
        "title": "t",
        "viewport": {"width": 1280, "height": 720},
        "tts": {"provider": "edge", "lang": "pl", "voice": "pl-PL-ZofiaNeural"},
    }
    a = Config.model_validate(base)
    b = Config.model_validate({**base, "holdFrameForNarration": False, "holdFrameSettle": 3.0})
    assert config_hash(a) == config_hash(b)


def test_desktop_config_default_is_navy_and_render_only():
    assert Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
        }
    ).desktop.color.startswith("#")


def test_desktop_config_does_not_change_the_config_hash():
    # The desktop step compiles to nothing, so its colour must not invalidate a
    # compiled sidecar.
    plain = _cfg()
    tinted = _cfg()
    tinted.desktop = DesktopConfig(color="#ff0000")
    assert config_hash(plain) == config_hash(tinted)


def test_fade_is_off_by_default_and_reads_the_in_out_aliases():
    assert not Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
        }
    ).fade.enabled

    fade = FadeConfig.model_validate({"enabled": True, "in": 0.4, "out": 1.2, "color": "white"})
    assert (fade.fade_in, fade.fade_out, fade.color, fade.audio) == (0.4, 1.2, "white", True)


def test_fade_rejects_a_negative_duration():
    with pytest.raises(ValidationError):
        FadeConfig.model_validate({"in": -0.1})


def test_fade_is_render_only_and_does_not_change_the_config_hash():
    # Toggling a fade must not invalidate a compiled sidecar.
    plain = _cfg()
    faded = _cfg()
    faded.fade = FadeConfig(enabled=True, **{"in": 1.0})
    assert config_hash(plain) == config_hash(faded)
