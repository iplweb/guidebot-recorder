"""Protokół providera TTS + cache pre-syntezy narracji (§8).

Klucz cache = hash z pełnej sekcji ``config.tts`` (provider/voice/lang/model/speed)
+ tekst + ``ttsAdapterVersion`` (wersja adaptera providera) + ``cacheSchemaVersion``.
Sam upgrade adaptera/providera przy niezmienionym ``config.tts`` również unieważnia
cache — dlatego wersje wchodzą do klucza jako salt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from guidebot_recorder.models.config import TtsConfig

#: wersja układu plików cache (nazwa/format meta). Zmiana → nowy klucz, pełna ressynteza.
CACHE_SCHEMA_VERSION = 1


@dataclass
class Segment:
    """Zsyntetyzowany fragment narracji: tekst, plik audio, długość w sekundach."""

    text: str
    path: Path
    duration: float


@runtime_checkable
class TtsProvider(Protocol):
    """Provider syntezy mowy.

    ``adapter_version`` uczestniczy w kluczu cache (§8) — bump przy zmianie
    zachowania syntezy przy niezmienionym ``config.tts``.
    """

    adapter_version: int

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        """Zsyntetyzuj ``text`` do pliku ``out``; zwróć długość w sekundach."""
        ...


def cache_key(
    text: str,
    tts: TtsConfig,
    adapter_version: int,
    cache_schema_version: int,
) -> str:
    """SHA-256 z kanonicznej projekcji: sekcja ``config.tts`` + tekst + wersje.

    Wrażliwy na provider/voice/lang/model/speed, treść oraz salt wersji
    (adapter + schemat cache).
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
    """Cache pre-syntezy na dysku: HIT z cache bez wołania providera (§8, Faza 0)."""

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
        """Zwróć segment z cache (HIT) lub zsyntetyzuj przez ``provider`` (MISS).

        HIT wymaga istnienia pliku audio **oraz** meta z długością — wtedy provider
        nie jest wołany. MISS: synteza, zapis meta z długością, zwrot segmentu.
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
