"""Scenario and Step (§3) — validates "exactly one command per step".

The source scenario carries intents only; resolved actions live in a separate
CompiledScenario (``*.compiled.yaml``), not inline on the step.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidebot_recorder.models.action import Expect, WaitState
from guidebot_recorder.models.config import Config

#: "primary" commands (an action/step); `say` may accompany one as narration
PRIMARY_COMMANDS = ("teach", "navigate", "click", "hover", "enter_text", "wait", "slide")


class EnterText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: str
    text: str


class WaitUntil(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: str
    state: WaitState = "visible"
    timeout: float = 10.0


class NavigateConfig(BaseModel):
    """Object form of ``navigate`` with an optional typing override."""

    model_config = ConfigDict(extra="forbid")

    url: str
    type: bool | None = None


class Slide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    subtitle: str | None = None
    notes: str | None = None
    hold: float = 2.5

    @model_validator(mode="after")
    def _at_least_one_text(self) -> "Slide":
        if not any((self.title, self.subtitle, self.notes)):
            raise ValueError("slide wymaga co najmniej jednego z: title/subtitle/notes")
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    say: str | None = None
    teach: str | None = None
    navigate: str | NavigateConfig | None = None
    click: str | None = None
    hover: str | None = None
    enter_text: EnterText | None = Field(default=None, alias="enterText")
    wait: float | WaitUntil | None = None
    slide: Slide | None = None
    expect: Expect | None = None
    translations: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_command(self) -> Step:
        present = [c for c in PRIMARY_COMMANDS if getattr(self, c) is not None]
        if len(present) > 1:
            raise ValueError(
                f"krok ma {len(present)} komend ({present}); dozwolona dokładnie jedna"
            )
        if len(present) == 0 and self.say is None:
            raise ValueError("krok bez komendy i bez `say` — pusty krok")
        return self

    def command_kind(self) -> str:
        for c in PRIMARY_COMMANDS:
            if getattr(self, c) is not None:
                return "enterText" if c == "enter_text" else c
        return "say"

    def requires_target(self) -> bool:
        kind = self.command_kind()
        if kind in ("teach", "enterText", "click", "hover"):
            return True
        if kind == "wait" and isinstance(self.wait, WaitUntil):
            return True
        return False

    def navigate_url(self) -> str | None:
        """Return the URL from either the legacy string or object form."""
        if isinstance(self.navigate, NavigateConfig):
            return self.navigate.url
        return self.navigate

    def navigate_type_override(self) -> bool | None:
        """Return the per-step typing override, if object ``navigate`` supplies one."""
        if isinstance(self.navigate, NavigateConfig):
            return self.navigate.type
        return None

    def narration(self) -> str | None:
        """Return the canonical/default narration without changing action intent."""

        if self.say:
            return self.say
        if self.teach:
            return self.teach
        return None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    steps: list[Step]

    @model_validator(mode="after")
    def _complete_audio_translations(self) -> Scenario:
        expected = {track.lang for track in self.config.audio_tracks}
        for index, step in enumerate(self.steps):
            actual = set(step.translations)
            if step.narration() is None:
                if actual:
                    languages = ", ".join(sorted(actual))
                    raise ValueError(
                        f"krok {index}: tłumaczenia bez narracji `say`/`teach`: {languages}"
                    )
                continue
            missing = expected - actual
            if missing:
                languages = ", ".join(sorted(missing))
                raise ValueError(f"krok {index}: brak tłumaczeń dla ścieżek: {languages}")
            unknown = actual - expected
            if unknown:
                languages = ", ".join(sorted(unknown))
                raise ValueError(f"krok {index}: niezdefiniowane tłumaczenia: {languages}")
        return self
