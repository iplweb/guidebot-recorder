"""Scenario config + config_hash (§3.1/§4.3)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidebot_recorder.languages import is_iso_639_2

#: version of the canonical config projection used for the hash
CONFIG_HASH_VERSION = 2


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


class CursorClick(BaseModel):
    """Appearance of the click ripple. Defaults reproduce today's hard-coded ring."""

    model_config = ConfigDict(extra="forbid")
    color: str = "rgba(37,99,235,.9)"          # today's ring colour (cursor.js:227)
    scale: float = Field(default=3.25, gt=0)   # today's end-scale (cursor.js:234); > 0
    flash: bool = False                        # opt-in filled disc under the ring


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

    # --- Click ripple appearance (render-only; injected into cursor.js) ---
    click: CursorClick = Field(default_factory=CursorClick)


class ChromeConfig(BaseModel):
    """Browser chrome (address bar shell) rendered around the recorded page.

    The whole feature is opt-in.  ``enabled`` and ``height`` change the compiled
    site layout — the page renders inside an iframe of height ``H - height`` — so
    those two are the *only* chrome fields folded into :func:`config_hash`.  The
    cosmetic and typing/interaction fields below are render-time visuals and stay
    outside the hash, so tweaking them never forces a recompile.
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

    # --- Typing / interaction in the address bar (render-only, off the hash) ---
    interact_on_navigate: bool = Field(default=True, alias="interactOnNavigate")
    char_delay_ms: int = Field(default=110, alias="charDelayMs")
    char_jitter_ms: int = Field(default=55, alias="charJitterMs")
    segment_pause_ms: int = Field(default=180, alias="segmentPauseMs")
    pre_navigate_pause_ms: int = Field(default=400, alias="preNavigatePauseMs")
    focus_color: str = Field(default="#3b82f6", alias="focusColor")
    show_caret: bool = Field(default=True, alias="showCaret")


class IntroConfig(BaseModel):
    """Render-only auto-intro title card (replaces the white bootstrap when enabled)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    subtitle: str | None = None
    notes: str | None = None


class SoundConfig(BaseModel):
    """Render-only, opt-in built-in SFX mixed under the narration."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    click: bool = True
    keys: bool = True
    # dB attenuation on the SFX bed; <= 0 only. A positive gain would erode the
    # −20 dBFS source headroom the clipping defence relies on.
    volume: float = Field(default=-12.0, le=0)


class TypingConfig(BaseModel):
    """Render-only character-by-character input animation."""

    model_config = ConfigDict(extra="forbid")
    animate: bool = False                  # opt-in; keeps existing renders inert
    # ms PER CHARACTER — a *delay* (higher = slower). NOT CursorConfig.speed, which is
    # a px/ms *rate* (higher = faster). Same word, inverted meaning; do not confuse.
    speed: int = Field(default=60, gt=0)


class PopupConfig(BaseModel):
    """Cosmetic settings for the floating popup-window presentation at ``render``.

    Purely visual — like :class:`CursorConfig`, these never affect the compiled
    targets, so they are *not* part of :func:`config_hash` and changing them does
    not require a recompile. Every field has a sensible default; omit the whole
    ``popup:`` block to keep the built-in look and motion.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    floating: bool = True
    scale: float = 0.72
    corner_radius: int = Field(default=14, alias="cornerRadius")
    shadow: bool = True
    backdrop_dim: float = Field(default=0.45, alias="backdropDim")
    backdrop_blur: int = Field(default=0, alias="backdropBlur")
    open_ms: int = Field(default=320, alias="openMs")
    close_ms: int = Field(default=240, alias="closeMs")

    # --- Transition mode. ``None`` derives from ``floating`` (back-compat) ---
    transition: Literal["cut", "float", "slide"] | None = None
    slide_ms: int = Field(default=400, alias="slideMs")

    @property
    def effective_transition(self) -> str:
        """Resolved transition mode.

        An explicit ``transition`` always wins; when unset it derives from the
        legacy ``floating`` flag (``True`` → ``"float"``, ``False`` → ``"cut"``).
        """

        return self.transition or ("float" if self.floating else "cut")

    @property
    def is_bare(self) -> bool:
        """Whether the popup is presented without browser chrome (float/slide)."""

        return self.effective_transition in ("float", "slide")


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
    typing: TypingConfig = Field(default_factory=TypingConfig)
    sound: SoundConfig = Field(default_factory=SoundConfig)
    intro: IntroConfig = Field(default_factory=IntroConfig)
    popup: PopupConfig = Field(default_factory=PopupConfig)

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


def site_viewport(cfg: Config) -> tuple[int, int]:
    """Layout viewport of the target site (width, height).

    When ``chrome.enabled`` is true the site renders inside the shell iframe of
    height ``H - chrome.height``, so both ``compile`` and ``render`` must resolve
    the site at the reduced height for the frozen positions to line up. With
    chrome disabled the site occupies the full configured viewport.
    """

    width = cfg.viewport.width
    height = cfg.viewport.height
    if cfg.chrome.enabled:
        height -= cfg.chrome.height
    return width, height


def config_hash(cfg: Config) -> str:
    """SHA-256 of the canonical projection: viewport, locale, tts.lang, chrome geometry.

    Changing the viewport/locale/default TTS language invalidates the references
    (fingerprint, §4.1). Alternate audio tracks and MP4 metadata are render-only.

    Chrome geometry (``chrome.enabled`` and ``chrome.height``) is included because
    enabling the chrome shell or resizing it shrinks the site's compile/layout
    viewport — the page renders inside an iframe of height ``H - chrome.height`` —
    which changes the compiled references. The cosmetic and typing/interaction
    chrome fields do not affect layout and are deliberately left out.
    """
    projection = {
        "v": CONFIG_HASH_VERSION,
        "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
        "locale": cfg.locale,
        "tts_lang": cfg.tts.lang,
        "chrome": {"enabled": cfg.chrome.enabled, "height": cfg.chrome.height},
    }
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
