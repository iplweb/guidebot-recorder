"""Scenario config + config_hash (§3.1/§4.3)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

#: version of the canonical config projection used for the hash
CONFIG_HASH_VERSION = 1


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int
    height: int


class TtsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    voice: str
    lang: str
    model: str | None = None
    speed: float | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    title: str
    viewport: Viewport
    tts: TtsConfig
    base_url: str | None = Field(default=None, alias="baseUrl")
    locale: str | None = None


def config_hash(cfg: Config) -> str:
    """SHA-256 of the canonical projection: viewport, locale, tts.lang.

    Changing the viewport/locale/TTS language invalidates the references (fingerprint, §4.1).
    """
    projection = {
        "v": CONFIG_HASH_VERSION,
        "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
        "locale": cfg.locale,
        "tts_lang": cfg.tts.lang,
    }
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
