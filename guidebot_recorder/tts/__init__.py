"""Narracja TTS: protokół providera, cache z kluczem wersjonowanym, edge-tts."""

from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    TtsProvider,
    cache_key,
)

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "Segment",
    "TtsCache",
    "TtsProvider",
    "cache_key",
]
