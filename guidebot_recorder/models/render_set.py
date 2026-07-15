"""Schema of a manifest grouping complete localized Guidebot scenarios."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CANONICAL_BCP47 = re.compile(
    r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*$"
)


class RenderSetVariant(BaseModel):
    """Source scenario and output path for one manifest language."""

    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(min_length=1)
    output: str = Field(min_length=1)


class LocalizedRenderSet(BaseModel):
    """Versioned localized render-set manifest."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["localized-render-set"]
    version: Literal[1]
    variants: dict[str, RenderSetVariant] = Field(min_length=1)

    @field_validator("version", mode="before")
    @classmethod
    def _integer_version(cls, version: object) -> object:
        if type(version) is not int:  # bool/1.0 must not masquerade as schema version 1
            raise ValueError("version musi być liczbą całkowitą 1")
        return version

    @field_validator("variants")
    @classmethod
    def _canonical_language_tags(
        cls, variants: dict[str, RenderSetVariant]
    ) -> dict[str, RenderSetVariant]:
        invalid = [language for language in variants if not _CANONICAL_BCP47.fullmatch(language)]
        if invalid:
            raise ValueError(
                "klucze wariantów muszą być kanonicznymi tagami BCP 47: " + ", ".join(invalid)
            )
        return variants
