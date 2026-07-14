"""Scenario i Step (§3) — walidator „dokładnie jedna komenda na krok"."""

from __future__ import annotations

from typing import Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidebot_recorder.models.action import CachedAction, Expect, WaitState
from guidebot_recorder.models.config import Config

#: komendy „główne" (akcja/krok); `say` może im towarzyszyć jako narracja
PRIMARY_COMMANDS = ("teach", "navigate", "click", "hover", "enter_text", "wait")


class EnterText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: str
    text: str


class WaitUntil(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: str
    state: WaitState = "visible"
    timeout: float = 10.0


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    say: str | None = None
    teach: str | None = None
    navigate: str | None = None
    click: str | None = None
    hover: str | None = None
    enter_text: EnterText | None = Field(default=None, alias="enterText")
    wait: Union[float, WaitUntil] | None = None
    expect: Expect | None = None
    cached_action: CachedAction | None = Field(default=None, alias="cachedAction")

    @model_validator(mode="after")
    def _exactly_one_command(self) -> "Step":
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


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Config
    steps: list[Step]
