"""CachedAction and Fingerprint — a frozen, versioned action (§4.2/§4.3)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import Target

#: reference schema version — a bump forces a re-resolve
COMPILER_VERSION = 2

ActionKind = Literal["click", "hover", "type", "waitFor"]
Expect = Literal["navigation", "idle", "none"]
WaitState = Literal["visible", "hidden", "enabled"]

_ENV_PLACEHOLDER = re.compile(r"\$\{\w+\}")
_SENSITIVE_INSTRUCTION = re.compile(
    r"\b(password|passwd|token|secret|sekret|hasł\w*|pin|otp|"
    r"api[\s_-]*key|klucz\w*\s+api|kod\w*\s+jednorazow\w*)\b",
    re.IGNORECASE,
)


def validate_teach_input_text(instruction: str, input_text: str) -> None:
    """Validate a literal before it is typed or persisted by a teach action."""

    if not input_text.strip():
        raise ValueError("reasoner nie zwrócił niepustego inputText dla akcji teach → type")
    if _ENV_PLACEHOLDER.search(input_text):
        raise ValueError("inputText zawiera placeholder ENV; użyj enterText")
    if input_text not in instruction:
        raise ValueError(
            "reasoner zwrócił inputText, który nie jest literalnym fragmentem instrukcji teach"
        )
    if _SENSITIVE_INSTRUCTION.search(instruction):
        raise ValueError("wartości wrażliwe wymagają enterText z ENV")


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
    #: absent for `waitFor: hidden` (nothing to compare against)
    identity: Identity | None = None
    expect: Expect
    state: WaitState | None = None
    #: observed during compile; render follows the new Page deterministically
    opens_popup: bool = False
    #: literal frozen by ``teach`` when the inferred action is ``type``
    input_text: str | None = None
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def _validate_behavioral_metadata(self) -> CachedAction:
        if self.opens_popup and self.action != "click":
            raise ValueError("opens_popup is only valid for click actions")
        if self.input_text is not None:
            if self.action != "type" or self.fingerprint.command_kind != "teach":
                raise ValueError("input_text is only valid for teach actions inferred as type")
            validate_teach_input_text(self.fingerprint.compiled_from, self.input_text)
        if (
            self.action == "type"
            and self.fingerprint.command_kind == "teach"
            and self.input_text is None
        ):
            raise ValueError("teach actions inferred as type require input_text")
        return self
