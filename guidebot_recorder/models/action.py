"""CachedAction i Fingerprint — zamrożona, wersjonowana akcja (§4.2/§4.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import Target

#: wersja schematu namiaru — wzrost wymusza re-resolve
COMPILER_VERSION = 1

ActionKind = Literal["click", "hover", "type", "waitFor"]
Expect = Literal["navigation", "idle", "none"]
WaitState = Literal["visible", "hidden", "enabled"]


class Fingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_kind: str
    compiled_from: str
    expect: Expect
    compiler_version: int = COMPILER_VERSION
    config_hash: str
    state: WaitState | None = None


class CachedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionKind
    target: Target
    #: brak dla `waitFor: hidden` (nie ma czego porównywać)
    identity: Identity | None = None
    expect: Expect
    state: WaitState | None = None
    fingerprint: Fingerprint
