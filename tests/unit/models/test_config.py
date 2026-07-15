import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    ChromeConfig,
    Config,
    TtsConfig,
    Viewport,
    config_hash,
)


def _cfg(w=1280, locale="pl-PL", chrome: ChromeConfig | None = None):
    return Config(
        title="t",
        viewport=Viewport(width=w, height=720),
        locale=locale,
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
        **({"chrome": chrome} if chrome is not None else {}),
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


def test_chrome_config_is_excluded_from_config_hash():
    baseline = _cfg()
    customized = _cfg(
        chrome=ChromeConfig(
            enabled=True,
            show_url=False,
            type_on_navigate=False,
            height=80,
            bar_color="#000",
            text_color="#fff",
            radius=20,
            show_lock=False,
            close_color="#100",
            minimize_color="#110",
            maximize_color="#010",
        )
    )

    assert config_hash(customized) == config_hash(baseline)
