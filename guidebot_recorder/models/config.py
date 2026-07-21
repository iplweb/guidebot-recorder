"""Scenario config + config_hash (§3.1/§4.3)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from guidebot_recorder.languages import is_iso_639_2

#: version of the canonical config projection used for the hash
CONFIG_HASH_VERSION = 2

# Lower bound for `Config.hold_frame_settle`, in seconds: two frames at the
# renderer's frame rate (guidebot_recorder.video.timeline.FPS = 25). Not
# imported from there — this model module should stay free of a dependency
# on the video package, so the value is restated here with FPS named as its
# source of truth.
#
# A settle below one frame is not representable on that grid at all: the
# renderer would claim to record real time it cannot place, and a zero settle
# means the picture stops before the step's own entry animation has drawn a
# single frame of itself. That argument only requires a ONE-frame floor —
# settle = 1/25 has been verified to render correctly (the freeze lands at
# narr + 1, and `video.timeline._segments` folds it into a run of two frames
# or more; nothing breaks). The second frame here is a deliberate, extra
# margin, not something the one-frame argument above demands on its own —
# kept because "smallest value that still works today" and "smallest value
# that will keep working as the renderer changes" are different bars, and the
# cost of the margin is one settle-frame of narration, not one video-frame of
# render time.
#
# This bound is NOT what keeps narration offsets from colliding. That was the
# original (mistaken) rationale: settle separates a step's start from its own
# freeze, and says nothing about the distance from that freeze to the NEXT
# step's stamp, which is where the collision actually happened. Monotonic
# stamping in `recorder.render._stamp_frame` is what fixes it, at every settle
# value including this floor.
MIN_HOLD_FRAME_SETTLE = 2 / 25


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
    color: str = "rgba(37,99,235,.9)"  # today's ring colour (cursor.js:227)
    scale: float = Field(default=3.25, gt=0)  # today's end-scale (cursor.js:234); > 0
    flash: bool = False  # opt-in filled disc under the ring


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
    # Perpendicular arc depth as a fraction of travel distance: the glide walks
    # a bowed path instead of a machine-straight line. 0 restores straight moves.
    bow: float = Field(default=0.12, ge=0)

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
    char_delay_ms: int = Field(default=60, alias="charDelayMs")
    char_jitter_ms: int = Field(default=55, alias="charJitterMs")
    segment_pause_ms: int = Field(default=180, alias="segmentPauseMs")
    # Hard ceiling on a single character's delay, as a multiple of
    # ``char_delay_ms`` — keeps the jitter tail from producing absurd stalls.
    max_delay_factor: float = Field(default=2.5, alias="maxDelayFactor", ge=1.0)
    pre_navigate_pause_ms: int = Field(default=400, alias="preNavigatePauseMs")
    focus_color: str = Field(default="#3b82f6", alias="focusColor")
    show_caret: bool = Field(default=True, alias="showCaret")


class IntroConfig(BaseModel):
    """Render-only auto-intro title card (replaces the white bootstrap when enabled)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    subtitle: str | None = None
    notes: str | None = None


class DesktopConfig(BaseModel):
    """Render-only appearance for the ``desktop`` opener step.

    Only the background colour lives here — it is a film-wide look, so every
    desktop step matches without repeating it. The icon and its caption are
    per-step (they can differ) and live on the step model. Not part of
    :func:`config_hash`: the desktop step compiles to nothing.
    """

    model_config = ConfigDict(extra="forbid")
    color: str = "#1f3a63"  # granatowy — a calm desktop navy


class FadeConfig(BaseModel):
    """Render-only fade from/to a flat colour at the film's two ends.

    Off by default: enabling it re-encodes the picture in the final mux (a fade
    cannot be applied to a copied stream), so a scenario that does not ask for
    one keeps today's output byte-identical. Not part of :func:`config_hash` —
    fades change no compiled reference, so toggling one needs no recompile.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    enabled: bool = False
    # ``in`` is a Python keyword, hence the alias; scenarios write `in:`.
    fade_in: float = Field(default=0.6, alias="in", ge=0)
    fade_out: float = Field(default=0.8, alias="out", ge=0)
    color: str = "black"
    # The narration is mixed to the full film length, so a picture that fades to
    # black over a still-audible voice reads as a glitch. Fading both is the
    # sane default; opt out when the bed is meant to run to the last sample.
    audio: bool = True


class SoundConfig(BaseModel):
    """Render-only built-in SFX mixed under the narration (on by default)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    click: bool = True
    keys: bool = True
    # User attenuation on the already-balanced SFX bed. Per-kind mouse/key gains
    # deliberately spend some of the −20 dBFS source headroom; positive user gain
    # stays forbidden and the final narration mix has its own limiter.
    volume: float = Field(default=-12.0, le=0)


class TypingConfig(BaseModel):
    """Render-only character-by-character input animation (on by default).

    Form fields are typed with the same natural feel as the address bar: a base
    per-character delay plus jitter. Set ``animate: false`` per scenario for
    masked/formatted/autocomplete fields where per-character replay could
    misrepresent the value (the final value is corrected regardless).
    """

    model_config = ConfigDict(extra="forbid")
    animate: bool = True
    # ms PER CHARACTER — a *delay* (higher = slower). NOT CursorConfig.speed, which is
    # a px/ms *rate* (higher = faster). Same word, inverted meaning; do not confuse.
    speed: int = Field(default=60, gt=0)
    # ± jitter (ms) around ``speed`` so form typing is natural, not metronomic —
    # matching the address-bar feel (ChromeConfig.char_jitter_ms).
    jitter_ms: int = Field(default=40, alias="jitterMs", ge=0)
    # Hard ceiling on a single character's delay, as a multiple of ``speed``
    # (same meaning as ChromeConfig.max_delay_factor).
    max_delay_factor: float = Field(default=2.5, alias="maxDelayFactor", ge=1.0)


class PopupConfig(BaseModel):
    """Cosmetic settings for the floating popup-window presentation at ``render``.

    Purely visual — like :class:`CursorConfig`, these never affect the compiled
    targets, so they are *not* part of :func:`config_hash` and changing them does
    not require a recompile. Every field has a sensible default; omit the whole
    ``popup:`` block to keep the built-in look and motion.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    floating: bool = True
    scale: float = 0.85
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


class SelectsConfig(BaseModel):
    """Settings for the DOM select shim that makes dropdown lists visible on camera.

    Purely visual for cosmetic fields — like :class:`CursorConfig`, these never
    affect the compiled targets, so only ``mode`` is part of :func:`config_hash`
    and only when it differs from the default. Changing ``settle_ms``,
    ``max_visible_options``, or ``open_hold_ms`` does not require a recompile.
    Every field has a sensible default; omit the whole ``selects:`` block to keep
    the built-in shim behaviour.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Global escape hatch: "shim" (default) replaces native selects with DOM overlays
    # so their option lists are visible on camera; "native" falls back to arrow-key
    # stepping when the shim cannot drive an enhanced widget.
    mode: Literal["shim", "native"] = "shim"
    # Milliseconds to wait after page load before classifying raw vs enhanced selects.
    # The page's own initialization (select2, Chosen, Tom Select) has this much time
    # to hide/enhance its original <select> before the shim classifies it.
    settle_ms: int = Field(default=1000, ge=1, alias="settleMs")
    # Number of options visible in the list at once before scrolling within it.
    max_visible_options: int = Field(default=8, ge=1, alias="maxVisibleOptions")
    # Milliseconds to hold the unfurled list open for the viewer to read it,
    # before the cursor moves to the chosen option.
    open_hold_ms: int = Field(default=350, ge=1, alias="openHoldMs")


class VerifyLoggedIn(BaseModel):
    """Post-setup login check: the recorded session is considered authenticated
    when ``contains_text`` is present on the page (optionally after visiting
    ``url``). A bare string in YAML is shorthand for ``{containsText: <str>}``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    contains_text: str = Field(alias="containsText")
    url: str | None = None
    timeout: float = 8


class Config(BaseModel):
    # `validate_assignment` because the render CLI's overrides (`--hold-frame`,
    # `--hold-frame-settle`) are applied by ASSIGNING onto an already-built
    # Config. Without it those assignments skip every constraint the field
    # declares: `--hold-frame-settle 0` reached the recorder despite the `ge=`
    # bound, and a negative value made the held frame LONGER than the narration
    # asked for, inflating the film past its own audio with no error. Validating
    # on assignment fixes the whole class at the model instead of at one call
    # site, so a future override cannot reintroduce it.
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)
    title: str
    viewport: Viewport
    tts: TtsConfig
    base_url: str | None = Field(default=None, alias="baseUrl")
    locale: str | None = None

    # --- Pre-recording setup (Phase A1) ---
    # Path to a setup scenario run once to establish an authenticated session
    # before the main recording. Only `setup` affects compiled references, so it
    # is the only one of these three folded into config_hash (and only when set,
    # keeping legacy scenarios at their current hash).
    setup: str | None = None
    verify_user_logged_in: VerifyLoggedIn | None = Field(default=None, alias="verifyUserLoggedIn")
    max_age_hours: float | None = Field(default=None, alias="maxAgeHours")
    audio_tracks: list[TtsConfig] = Field(default_factory=list, alias="audioTracks")
    cursor: CursorConfig = Field(default_factory=CursorConfig)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)
    typing: TypingConfig = Field(default_factory=TypingConfig)
    sound: SoundConfig = Field(default_factory=SoundConfig)
    intro: IntroConfig = Field(default_factory=IntroConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    fade: FadeConfig = Field(default_factory=FadeConfig)
    popup: PopupConfig = Field(default_factory=PopupConfig)
    selects: SelectsConfig = Field(default_factory=SelectsConfig)

    # --- Render pacing (render-only; deliberately absent from config_hash) ---
    # Holding a still frame instead of waiting out the voice-over. The narration
    # still plays in full; it is the picture that stops. `hold` matches the sense
    # it already carries in `step.slide.hold`.
    hold_frame_for_narration: bool = Field(default=True, alias="holdFrameForNarration")
    # Real seconds recorded before the frame is held, paid OUT OF the narration
    # (not on top of it) so the finished film keeps its length. Gives entry
    # animations triggered by this step time to finish before the picture stops.
    # Floor is MIN_HOLD_FRAME_SETTLE (two 25fps frames): below it the settle is
    # not representable on the frame grid (see that constant's comment).
    hold_frame_settle: float = Field(default=1.0, alias="holdFrameSettle", ge=MIN_HOLD_FRAME_SETTLE)

    @field_validator("verify_user_logged_in", mode="before")
    @classmethod
    def _wrap_verify_shorthand(cls, value: object) -> object:
        """Accept a bare string as ``{containsText: <str>}`` shorthand."""
        if isinstance(value, str):
            return {"containsText": value}
        return value

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

    The select shim's ``mode`` is included when it differs from the default "shim":
    changing it to "native" changes what the resolver drives, so existing scenarios
    must keep their current hash to avoid forced recompilation. Cosmetic fields
    (``settle_ms``, ``max_visible_options``, ``open_hold_ms``) stay out of the
    projection, consistent with other cosmetic chrome and cursor fields.
    """
    projection = {
        "v": CONFIG_HASH_VERSION,
        "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
        "locale": cfg.locale,
        "tts_lang": cfg.tts.lang,
        "chrome": {"enabled": cfg.chrome.enabled, "height": cfg.chrome.height},
    }
    # A configured setup scenario changes the authenticated state the site is
    # compiled against, so it belongs in the hash — but only when set, so legacy
    # scenarios (setup unset) keep their pre-existing hash. `verify_user_logged_in`
    # and `max_age_hours` are run-time gating only and never enter the projection.
    if cfg.setup is not None:
        projection["setup"] = cfg.setup
    # Selects mode affects resolution and compilation only when it differs from
    # the default "shim", so it is included like setup: only non-default values
    # change the hash, keeping legacy scenarios stable.
    if cfg.selects.mode != "shim":
        projection["selects_mode"] = cfg.selects.mode
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
