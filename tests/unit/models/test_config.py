import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    MIN_HOLD_FRAME_SETTLE,
    ChromeConfig,
    Config,
    DesktopConfig,
    FadeConfig,
    PopupConfig,
    TtsConfig,
    Viewport,
    config_hash,
)


def _cfg(
    w=1280,
    locale="pl-PL",
    chrome: ChromeConfig | None = None,
    popup: PopupConfig | None = None,
):
    return Config(
        title="t",
        viewport=Viewport(width=w, height=720),
        locale=locale,
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
        **({"chrome": chrome} if chrome is not None else {}),
        **({"popup": popup} if popup is not None else {}),
    )


def test_hash_stable():
    assert config_hash(_cfg()) == config_hash(_cfg())


def test_hash_changes_on_viewport():
    assert config_hash(_cfg(w=1280)) != config_hash(_cfg(w=768))


def test_hash_changes_on_locale():
    assert config_hash(_cfg(locale="pl-PL")) != config_hash(_cfg(locale="en-US"))


def test_hash_changes_on_default_tts_language():
    baseline = _cfg()
    changed = baseline.model_copy(update={"tts": baseline.tts.model_copy(update={"lang": "en-US"})})

    assert config_hash(changed) != config_hash(baseline)


def test_base_url_alias_from_yaml():
    # spec §3.1/§3.2 uses `baseUrl:` in YAML
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1, "height": 1},
            "tts": {"provider": "e", "voice": "v", "lang": "pl"},
            "baseUrl": "https://app.example.com",
        }
    )
    assert cfg.base_url == "https://app.example.com"


def test_multilingual_audio_tracks_accept_titles_and_yaml_alias():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {
                "provider": "edge",
                "voice": "pl-PL-MarekNeural",
                "lang": "pl-PL",
                "title": "Polski",
                "trackLanguage": "pol",
            },
            "audioTracks": [
                {
                    "provider": "edge",
                    "voice": "en-US-GuyNeural",
                    "lang": "en-US",
                    "title": "English",
                    "trackLanguage": "eng",
                }
            ],
        }
    )

    assert cfg.tts.title == "Polski"
    assert cfg.tts.mp4_language() == "pol"
    assert [track.lang for track in cfg.audio_tracks] == ["en-US"]
    assert cfg.audio_tracks[0].title == "English"


def test_multilingual_audio_tracks_reject_duplicate_languages():
    with pytest.raises(ValidationError, match="unikalny język"):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"provider": "edge", "voice": "v1", "lang": "pl-PL"},
                "audioTracks": [
                    {
                        "provider": "edge",
                        "voice": "v2",
                        "lang": "pl-PL",
                        "trackLanguage": "pol",
                    }
                ],
            }
        )


def test_audio_track_metadata_is_excluded_from_config_hash():
    baseline = _cfg()
    multilingual = baseline.model_copy(
        update={
            "tts": baseline.tts.model_copy(update={"title": "Polski", "track_language": "pol"}),
            "audio_tracks": [
                TtsConfig(
                    provider="edge",
                    voice="en-US-GuyNeural",
                    lang="en-US",
                    title="English",
                    trackLanguage="eng",
                )
            ],
        }
    )

    assert config_hash(multilingual) == config_hash(baseline)


def test_multilingual_audio_requires_iso_639_track_languages():
    with pytest.raises(ValidationError, match="wymaga `trackLanguage`.*pl-PL"):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"provider": "edge", "voice": "pl", "lang": "pl-PL"},
                "audioTracks": [
                    {
                        "provider": "edge",
                        "voice": "en",
                        "lang": "en-US",
                        "trackLanguage": "eng",
                    }
                ],
            }
        )


def test_multilingual_audio_rejects_non_iso_639_track_language():
    with pytest.raises(ValidationError, match="zarejestrowanym kodem ISO 639-2.*en-US"):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {
                    "provider": "edge",
                    "voice": "pl",
                    "lang": "pl-PL",
                    "trackLanguage": "pol",
                },
                "audioTracks": [
                    {
                        "provider": "edge",
                        "voice": "en",
                        "lang": "en-US",
                        "trackLanguage": "en-US",
                    }
                ],
            }
        )


def test_single_audio_track_rejects_non_iso_639_track_language():
    with pytest.raises(ValidationError, match="zarejestrowanym kodem ISO 639-2.*pl-PL"):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {
                    "provider": "edge",
                    "voice": "pl",
                    "lang": "pl-PL",
                    "trackLanguage": "pl-PL",
                },
            }
        )


def test_audio_track_rejects_unregistered_three_letter_language():
    with pytest.raises(ValidationError, match="kodem ISO 639-2.*xyz"):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {
                    "provider": "edge",
                    "voice": "pl",
                    "lang": "pl-PL",
                    "trackLanguage": "xyz",
                },
            }
        )


def test_chrome_defaults_to_disabled_without_changing_legacy_config():
    cfg = _cfg()

    assert cfg.chrome == ChromeConfig()
    assert cfg.chrome.enabled is False
    assert cfg.chrome.show_url is True
    assert cfg.chrome.type_on_navigate is True


def test_chrome_config_accepts_yaml_aliases_and_cosmetic_fields():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "chrome": {
                "enabled": True,
                "showUrl": False,
                "typeOnNavigate": False,
                "height": 64,
                "barColor": "#101010",
                "textColor": "#fefefe",
                "radius": 16,
                "showLock": False,
                "closeColor": "red",
                "minimizeColor": "yellow",
                "maximizeColor": "green",
            },
        }
    )

    assert cfg.chrome == ChromeConfig(
        enabled=True,
        show_url=False,
        type_on_navigate=False,
        height=64,
        bar_color="#101010",
        text_color="#fefefe",
        radius=16,
        show_lock=False,
        close_color="red",
        minimize_color="yellow",
        maximize_color="green",
    )


def test_chrome_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChromeConfig.model_validate({"enabled": True, "unknown": "value"})


def test_cosmetic_chrome_fields_are_excluded_from_config_hash():
    # Same enabled/height (the only two geometry fields that reach the hash),
    # everything else cosmetic/typing — the hash must stay put.
    baseline = _cfg(chrome=ChromeConfig(enabled=True, height=56))
    customized = _cfg(
        chrome=ChromeConfig(
            enabled=True,
            height=56,
            show_url=False,
            type_on_navigate=False,
            bar_color="#000",
            text_color="#fff",
            radius=20,
            show_lock=False,
            close_color="#100",
            minimize_color="#110",
            maximize_color="#010",
            interact_on_navigate=False,
            char_delay_ms=1,
            char_jitter_ms=1,
            segment_pause_ms=1,
            pre_navigate_pause_ms=1,
            focus_color="#000000",
            show_caret=False,
        )
    )

    assert config_hash(customized) == config_hash(baseline)


def test_new_chrome_typing_fields_defaults():
    chrome = ChromeConfig()

    assert chrome.interact_on_navigate is True
    assert chrome.char_delay_ms == 60
    assert chrome.char_jitter_ms == 55
    assert chrome.segment_pause_ms == 180
    assert chrome.pre_navigate_pause_ms == 400
    assert chrome.focus_color == "#3b82f6"
    assert chrome.show_caret is True


def test_new_chrome_typing_fields_parse_from_camelcase_aliases():
    chrome = ChromeConfig.model_validate(
        {
            "interactOnNavigate": False,
            "charDelayMs": 42,
            "charJitterMs": 7,
            "segmentPauseMs": 90,
            "preNavigatePauseMs": 250,
            "focusColor": "#abcdef",
            "showCaret": False,
        }
    )

    assert chrome.interact_on_navigate is False
    assert chrome.char_delay_ms == 42
    assert chrome.char_jitter_ms == 7
    assert chrome.segment_pause_ms == 90
    assert chrome.pre_navigate_pause_ms == 250
    assert chrome.focus_color == "#abcdef"
    assert chrome.show_caret is False


def test_new_chrome_typing_fields_parse_from_snake_case():
    chrome = ChromeConfig.model_validate(
        {
            "interact_on_navigate": False,
            "char_delay_ms": 42,
            "char_jitter_ms": 7,
            "segment_pause_ms": 90,
            "pre_navigate_pause_ms": 250,
            "focus_color": "#abcdef",
            "show_caret": False,
        }
    )

    assert chrome.interact_on_navigate is False
    assert chrome.char_delay_ms == 42
    assert chrome.char_jitter_ms == 7
    assert chrome.segment_pause_ms == 90
    assert chrome.pre_navigate_pause_ms == 250
    assert chrome.focus_color == "#abcdef"
    assert chrome.show_caret is False


def test_config_hash_changes_when_chrome_enabled_flips():
    disabled = _cfg(chrome=ChromeConfig(enabled=False))
    enabled = _cfg(chrome=ChromeConfig(enabled=True))

    assert config_hash(enabled) != config_hash(disabled)


def test_config_hash_changes_when_chrome_height_changes():
    short = _cfg(chrome=ChromeConfig(enabled=True, height=56))
    tall = _cfg(chrome=ChromeConfig(enabled=True, height=72))

    assert config_hash(short) != config_hash(tall)


def test_config_hash_unchanged_when_cosmetic_chrome_field_changes():
    baseline = _cfg(chrome=ChromeConfig(enabled=True))
    recolored = _cfg(chrome=ChromeConfig(enabled=True, bar_color="#123456"))

    assert config_hash(recolored) == config_hash(baseline)


def test_config_hash_unchanged_when_typing_chrome_field_changes():
    baseline = _cfg(chrome=ChromeConfig(enabled=True))
    faster = _cfg(chrome=ChromeConfig(enabled=True, char_delay_ms=10))

    assert config_hash(faster) == config_hash(baseline)


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


def test_popup_defaults():
    popup = PopupConfig()

    assert popup.floating is True
    assert popup.scale == 0.85
    assert popup.corner_radius == 14
    assert popup.shadow is True
    assert popup.backdrop_dim == 0.45
    assert popup.backdrop_blur == 0
    assert popup.open_ms == 320
    assert popup.close_ms == 240


def test_config_defaults_popup_to_built_in():
    cfg = _cfg()

    assert cfg.popup == PopupConfig()
    assert cfg.popup.floating is True


def test_popup_config_parses_from_camelcase_aliases():
    popup = PopupConfig.model_validate(
        {
            "floating": False,
            "scale": 0.5,
            "cornerRadius": 20,
            "shadow": False,
            "backdropDim": 0.6,
            "backdropBlur": 8,
            "openMs": 500,
            "closeMs": 300,
        }
    )

    assert popup.floating is False
    assert popup.scale == 0.5
    assert popup.corner_radius == 20
    assert popup.shadow is False
    assert popup.backdrop_dim == 0.6
    assert popup.backdrop_blur == 8
    assert popup.open_ms == 500
    assert popup.close_ms == 300


def test_popup_config_parses_from_snake_case():
    popup = PopupConfig.model_validate(
        {
            "floating": False,
            "scale": 0.5,
            "corner_radius": 20,
            "shadow": False,
            "backdrop_dim": 0.6,
            "backdrop_blur": 8,
            "open_ms": 500,
            "close_ms": 300,
        }
    )

    assert popup.floating is False
    assert popup.scale == 0.5
    assert popup.corner_radius == 20
    assert popup.shadow is False
    assert popup.backdrop_dim == 0.6
    assert popup.backdrop_blur == 8
    assert popup.open_ms == 500
    assert popup.close_ms == 300


def test_popup_config_from_config_yaml_alias():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "popup": {"cornerRadius": 20, "backdropDim": 0.6, "openMs": 500},
        }
    )

    assert cfg.popup.corner_radius == 20
    assert cfg.popup.backdrop_dim == 0.6
    assert cfg.popup.open_ms == 500


def test_popup_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"floating": True, "unknown": "value"})


def test_config_rejects_unknown_popup_fields():
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
                "popup": {"unknown": "value"},
            }
        )


def test_config_hash_unchanged_when_any_popup_field_changes():
    baseline = _cfg(popup=PopupConfig())
    customized = _cfg(
        popup=PopupConfig(
            floating=False,
            scale=0.5,
            corner_radius=20,
            shadow=False,
            backdrop_dim=0.6,
            backdrop_blur=8,
            open_ms=500,
            close_ms=300,
        )
    )

    assert config_hash(customized) == config_hash(baseline)


def test_popup_transition_defaults():
    popup = PopupConfig()

    assert popup.transition is None
    assert popup.slide_ms == 400


@pytest.mark.parametrize("mode", ["cut", "float", "slide"])
def test_popup_transition_parses_each_literal(mode):
    popup = PopupConfig.model_validate({"transition": mode})

    assert popup.transition == mode


def test_popup_slide_ms_parses_from_camelcase_alias():
    popup = PopupConfig.model_validate({"slideMs": 900})

    assert popup.slide_ms == 900


def test_popup_slide_ms_parses_from_snake_case():
    popup = PopupConfig.model_validate({"slide_ms": 900})

    assert popup.slide_ms == 900


def test_popup_rejects_invalid_transition():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"transition": "fade"})


def test_popup_still_forbids_unknown_fields_with_transition():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"transition": "slide", "unknown": "value"})


def test_effective_transition_derives_from_floating_when_unset():
    assert PopupConfig(floating=True).effective_transition == "float"
    assert PopupConfig(floating=False).effective_transition == "cut"


@pytest.mark.parametrize(
    ("floating", "transition", "expected"),
    [
        (False, "float", "float"),
        (True, "cut", "cut"),
        (True, "slide", "slide"),
        (False, "slide", "slide"),
        (True, "float", "float"),
        (False, "cut", "cut"),
    ],
)
def test_effective_transition_explicit_always_wins(floating, transition, expected):
    popup = PopupConfig(floating=floating, transition=transition)

    assert popup.effective_transition == expected


@pytest.mark.parametrize(
    ("floating", "transition", "expected"),
    [
        # driven via floating (transition unset)
        (True, None, True),
        (False, None, False),
        # driven via explicit transition
        (False, "float", True),
        (False, "slide", True),
        (True, "cut", False),
    ],
)
def test_is_bare_matrix(floating, transition, expected):
    popup = PopupConfig(floating=floating, transition=transition)

    assert popup.is_bare is expected


def test_config_hash_unchanged_when_transition_changes():
    baseline = _cfg(popup=PopupConfig(transition="cut"))
    changed = _cfg(popup=PopupConfig(transition="slide"))

    assert config_hash(changed) == config_hash(baseline)


def test_config_hash_unchanged_when_slide_ms_changes():
    baseline = _cfg(popup=PopupConfig(slide_ms=400))
    changed = _cfg(popup=PopupConfig(slide_ms=900))

    assert config_hash(changed) == config_hash(baseline)


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


# Task 1: SelectsConfig tests
def test_selects_config_defaults():
    from guidebot_recorder.models.config import SelectsConfig

    s = SelectsConfig()
    assert s.mode == "shim"
    assert s.settle_ms == 1000
    assert s.max_visible_options == 8
    assert s.open_hold_ms == 350


def test_selects_config_accepts_camelcase_and_snake_case_aliases():
    from guidebot_recorder.models.config import SelectsConfig

    s_camel = SelectsConfig.model_validate(
        {"mode": "native", "settleMs": 500, "maxVisibleOptions": 4, "openHoldMs": 200}
    )
    s_snake = SelectsConfig.model_validate(
        {"mode": "native", "settle_ms": 500, "max_visible_options": 4, "open_hold_ms": 200}
    )

    assert s_camel.mode == "native"
    assert s_camel.settle_ms == 500
    assert s_camel.max_visible_options == 4
    assert s_camel.open_hold_ms == 200
    assert s_snake.mode == "native"
    assert s_snake.settle_ms == 500
    assert s_snake.max_visible_options == 4
    assert s_snake.open_hold_ms == 200


def test_selects_config_rejects_unknown_fields():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig.model_validate({"mode": "shim", "unknown": "value"})


def test_selects_config_rejects_invalid_mode():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(mode="invalid")  # type: ignore


def test_selects_config_rejects_negative_settle_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(settle_ms=-1)


def test_selects_config_accepts_zero_settle_ms():
    """`0` means "no settle window", which is a legitimate thing to ask for.

    The floor used to be 1 ms, so the one page shape the window exists to
    accommodate — a site with no widget library at all, where waiting only
    delays every classification pass — had no way to switch it off. The widget
    already clamps at zero and simply schedules the pass on the next task.
    """

    from guidebot_recorder.models.config import SelectsConfig

    assert SelectsConfig(settle_ms=0).settle_ms == 0


def test_selects_config_rejects_max_visible_options_zero():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(max_visible_options=0)


def test_selects_config_rejects_negative_max_visible_options():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(max_visible_options=-1)


def test_selects_config_rejects_negative_open_hold_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(open_hold_ms=-1)


def test_selects_config_rejects_zero_open_hold_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(open_hold_ms=0)


def test_config_defaults_selects_to_built_in():
    cfg = _cfg()

    from guidebot_recorder.models.config import SelectsConfig

    assert cfg.selects == SelectsConfig()
    assert cfg.selects.mode == "shim"
    assert cfg.selects.settle_ms == 1000


def test_selects_config_from_config_yaml():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "selects": {"mode": "native", "settleMs": 500, "maxVisibleOptions": 5},
        }
    )

    assert cfg.selects.mode == "native"
    assert cfg.selects.settle_ms == 500
    assert cfg.selects.max_visible_options == 5
    assert cfg.selects.open_hold_ms == 350  # default


def test_config_hash_stable_with_default_selects():
    """Default selects must not change the hash to keep legacy scenarios stable."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    with_default_selects = _cfg()
    with_default_selects.selects = SelectsConfig()

    assert config_hash(baseline) == config_hash(with_default_selects)


def test_config_hash_changes_when_selects_mode_is_native():
    """Changing mode from default shim to native must invalidate the hash."""
    from guidebot_recorder.models.config import SelectsConfig

    default_mode = _cfg()
    native_mode = _cfg()
    native_mode.selects = SelectsConfig(mode="native")

    assert config_hash(default_mode) != config_hash(native_mode)


def test_config_hash_unchanged_when_selects_settle_ms_changes():
    """Changing settle_ms must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(settle_ms=500)

    assert config_hash(baseline) == config_hash(tweaked)


def test_config_hash_unchanged_when_selects_max_visible_options_changes():
    """Changing max_visible_options must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(max_visible_options=4)

    assert config_hash(baseline) == config_hash(tweaked)


def test_config_hash_unchanged_when_selects_open_hold_ms_changes():
    """Changing open_hold_ms must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(open_hold_ms=200)

    assert config_hash(baseline) == config_hash(tweaked)
