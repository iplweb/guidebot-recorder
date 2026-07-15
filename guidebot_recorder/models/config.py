"""Scenario config + config_hash (§3.1/§4.3)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidebot_recorder.languages import is_iso_639_2

#: version of the canonical config projection used for the hash
CONFIG_HASH_VERSION = 1


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int
    height: int


class TtsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    provider: str
    voice: str
    lang: str
    model: str | None = None
    speed: float | None = None
    # MP4 stream metadata only; neither field changes synthesized audio.
    title: str | None = None
    track_language: str | None = Field(default=None, alias="trackLanguage")

    def mp4_language(self) -> str:
        """Return the language tag written to the MP4 audio stream."""

        return self.track_language or "und"


class CursorConfig(BaseModel):
    """Cosmetic settings for the synthetic cursor shown during ``render``.

    Purely visual — these never affect the compiled targets, so they are *not*
    part of :func:`config_hash` and changing them does not require a recompile.
    Every field has a sensible default; omit the whole ``cursor:`` block to keep
    the built-in look and motion.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # --- Appearance (injected into cursor.js). width:height default keeps 3:4 ---
    width: int = 34
    height: int = 46
    color: str = "#ef4444"  # arrow fill
    outline: str = "#ffffff"  # arrow stroke — reads on any background
    glow: str = "rgba(239,68,68,.75)"  # halo, aids tracking while moving
    easing: str = "cubic-bezier(.45,.05,.25,1)"  # glide curve (ease-in-out)

    # --- Motion timing (Python side). Duration scales with travel distance ---
    speed: float = 1.15  # px per ms — higher = faster glide
    min_duration: float = Field(default=320.0, alias="minDuration")  # ms floor
    max_duration: float = Field(default=1400.0, alias="maxDuration")  # ms ceiling
    settle: float = 280.0  # ms pause after arrival, before the action fires


class ChromeConfig(BaseModel):
    """Cosmetic browser chrome rendered above the recorded page.

    The whole feature is opt-in.  These values only affect render-time visuals,
    so they deliberately stay outside :func:`config_hash`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    show_url: bool = Field(default=True, alias="showUrl")
    type_on_navigate: bool = Field(default=True, alias="typeOnNavigate")
    height: int = Field(default=56, gt=0)
    bar_color: str = Field(default="#f3f4f6", alias="barColor")
    text_color: str = Field(default="#374151", alias="textColor")
    radius: int = Field(default=12, ge=0)
    show_lock: bool = Field(default=True, alias="showLock")
    close_color: str = Field(default="#ff5f57", alias="closeColor")
    minimize_color: str = Field(default="#febc2e", alias="minimizeColor")
    maximize_color: str = Field(default="#28c840", alias="maximizeColor")


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    title: str
    viewport: Viewport
    tts: TtsConfig
    base_url: str | None = Field(default=None, alias="baseUrl")
    locale: str | None = None
    audio_tracks: list[TtsConfig] = Field(default_factory=list, alias="audioTracks")
    cursor: CursorConfig = Field(default_factory=CursorConfig)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)

    @model_validator(mode="after")
    def _unique_audio_languages(self) -> Config:
        tracks = [self.tts, *self.audio_tracks]
        languages = [track.lang for track in tracks]
        if len(languages) != len(set(languages)):
            raise ValueError("każda ścieżka audio musi mieć unikalny język `lang`")
        invalid = [
            track.track_language
            for track in tracks
            if track.track_language is not None and not is_iso_639_2(track.track_language)
        ]
        if invalid:
            raise ValueError(
                "`trackLanguage` musi być zarejestrowanym kodem ISO 639-2: " + ", ".join(invalid)
            )
        if self.audio_tracks:
            missing = [track.lang for track in tracks if track.track_language is None]
            if missing:
                raise ValueError(
                    "wielojęzyczne MP4 wymaga `trackLanguage` (ISO 639-2) dla: "
                    + ", ".join(missing)
                )
            mux_languages = [track.track_language for track in tracks]
            if len(mux_languages) != len(set(mux_languages)):
                raise ValueError("każda ścieżka audio musi mieć unikalny `trackLanguage`")
        return self


def config_hash(cfg: Config) -> str:
    """SHA-256 of the canonical projection: viewport, locale, tts.lang.

    Changing the viewport/locale/default TTS language invalidates the references
    (fingerprint, §4.1). Alternate audio tracks and MP4 metadata are render-only.
    """
    projection = {
        "v": CONFIG_HASH_VERSION,
        "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
        "locale": cfg.locale,
        "tts_lang": cfg.tts.lang,
    }
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
