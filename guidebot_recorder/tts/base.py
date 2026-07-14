"""TTS provider protocol + narration pre-synthesis cache (§8).

Cache key = hash of the full ``config.tts`` section (provider/voice/lang/model/speed)
+ text + ``ttsAdapterVersion`` (provider adapter version) + ``cacheSchemaVersion``.
Upgrading the adapter/provider alone, with ``config.tts`` unchanged, also invalidates
the cache — which is why the versions enter the key as salt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from guidebot_recorder.models.config import TtsConfig

#: cache file layout version (meta name/format). A change → new key, full re-synthesis.
CACHE_SCHEMA_VERSION = 1


@dataclass
class Segment:
    """A synthesized narration segment: text, audio file, duration in seconds."""

    text: str
    path: Path
    duration: float


@runtime_checkable
class TtsProvider(Protocol):
    """Speech synthesis provider.

    ``adapter_version`` participates in the cache key (§8) — bump it when the
    synthesis behavior changes while ``config.tts`` stays unchanged.
    """

    adapter_version: int

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        """Synthesize ``text`` into the file ``out``; return the duration in seconds."""
        ...


def cache_key(
    text: str,
    tts: TtsConfig,
    adapter_version: int,
    cache_schema_version: int,
) -> str:
    """SHA-256 of the canonical projection: the ``config.tts`` section + text + versions.

    Sensitive to provider/voice/lang/model/speed, the content, and the version salt
    (adapter + cache schema).
    """
    projection = {
        "adapter_version": adapter_version,
        "cache_schema_version": cache_schema_version,
        "text": text,
        "tts": {
            "provider": tts.provider,
            "voice": tts.voice,
            "lang": tts.lang,
            "model": tts.model,
            "speed": tts.speed,
        },
    }
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TtsCache:
    """On-disk pre-synthesis cache: a cache HIT skips calling the provider (§8, Phase 0)."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)

    def _audio_path(self, key: str) -> Path:
        return self.dir / f"{key}.mp3"

    def _meta_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    async def get_or_synth(
        self,
        text: str,
        tts: TtsConfig,
        provider: TtsProvider,
    ) -> Segment:
        """Return a segment from the cache (HIT) or synthesize it via ``provider`` (MISS).

        A HIT requires both the audio file **and** the meta with a duration to exist —
        only then is the provider skipped. MISS: synthesize, write the meta with the
        duration, and return the segment.
        """
        key = cache_key(text, tts, provider.adapter_version, CACHE_SCHEMA_VERSION)
        audio = self._audio_path(key)
        meta = self._meta_path(key)

        if audio.exists() and meta.exists():
            data = json.loads(meta.read_text(encoding="utf-8"))
            return Segment(text=text, path=audio, duration=float(data["duration"]))

        self.dir.mkdir(parents=True, exist_ok=True)
        duration = await provider.synth(text, tts, audio)
        meta.write_text(
            json.dumps(
                {"duration": duration, "text": text, "key": key},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return Segment(text=text, path=audio, duration=duration)
