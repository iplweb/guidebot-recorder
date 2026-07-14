from guidebot_recorder.models.config import Config, TtsConfig, Viewport, config_hash


def _cfg(w=1280, locale="pl-PL"):
    return Config(
        title="t",
        viewport=Viewport(width=w, height=720),
        locale=locale,
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
    )


def test_hash_stable():
    assert config_hash(_cfg()) == config_hash(_cfg())


def test_hash_changes_on_viewport():
    assert config_hash(_cfg(w=1280)) != config_hash(_cfg(w=768))


def test_hash_changes_on_locale():
    assert config_hash(_cfg(locale="pl-PL")) != config_hash(_cfg(locale="en-US"))
