"""Rdzeń `Config`: stabilność `config_hash()`, alias `baseUrl` i ścieżki audio.

Tu mieszka to, co dotyczy samego `Config` jako całości — co wchodzi do hasha
(viewport, locale, język TTS), jak YAML-owy `baseUrl` trafia do `base_url` oraz
walidacja wielojęzycznych ścieżek audio (tytuły, duplikaty, kody ISO 639-2).
Bloki konfiguracji poszczególnych podsystemów są w sąsiednich plikach
`test_config_chrome.py`, `test_config_media.py`, `test_config_popup.py`
i `test_config_selects.py`.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    Config,
    TtsConfig,
    config_hash,
)

from ._config_helpers import _cfg


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
