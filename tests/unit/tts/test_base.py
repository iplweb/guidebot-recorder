"""Tests for the provider protocol, cache key, and TtsCache (Task 14) — FAKE provider, no network."""

from __future__ import annotations

from pathlib import Path

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.tts.base import (
    CACHE_SCHEMA_VERSION,
    Segment,
    TtsCache,
    cache_key,
)


def _tts(voice: str = "pl-PL-ZofiaNeural") -> TtsConfig:
    return TtsConfig(provider="edge", voice=voice, lang="pl-PL")


class FakeProvider:
    """Network-free provider: writes a deterministic file and counts calls."""

    adapter_version = 1

    def __init__(self) -> None:
        self.calls = 0

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        self.calls += 1
        out.write_bytes(b"FAKE-AUDIO:" + text.encode("utf-8"))
        return 1.0


# --- cache_key ---


def test_cache_key_stable():
    assert cache_key("cześć", _tts(), 1, CACHE_SCHEMA_VERSION) == cache_key(
        "cześć", _tts(), 1, CACHE_SCHEMA_VERSION
    )


def test_cache_key_sensitive_to_voice():
    k1 = cache_key("cześć", _tts(voice="A"), 1, CACHE_SCHEMA_VERSION)
    k2 = cache_key("cześć", _tts(voice="B"), 1, CACHE_SCHEMA_VERSION)
    assert k1 != k2


def test_cache_key_sensitive_to_text():
    assert cache_key("a", _tts(), 1, 1) != cache_key("b", _tts(), 1, 1)


def test_cache_key_sensitive_to_adapter_version():
    assert cache_key("a", _tts(), 1, 1) != cache_key("a", _tts(), 2, 1)


def test_cache_key_sensitive_to_schema_version():
    assert cache_key("a", _tts(), 1, 1) != cache_key("a", _tts(), 1, 2)


def test_cache_key_ignores_mp4_track_metadata():
    baseline = _tts()
    metadata = baseline.model_copy(update={"title": "Polski", "track_language": "pol"})

    assert cache_key("a", baseline, 1, 1) == cache_key("a", metadata, 1, 1)


# --- TtsCache ---


async def test_miss_then_hit_does_not_recall_provider(tmp_path):
    cache = TtsCache(tmp_path)
    provider = FakeProvider()

    seg = await cache.get_or_synth("witaj", _tts(), provider)
    assert isinstance(seg, Segment)
    assert seg.text == "witaj"
    assert seg.duration == 1.0
    assert seg.path.exists()
    assert provider.calls == 1

    # HIT: the second call does not invoke the provider and returns the same file/duration
    seg2 = await cache.get_or_synth("witaj", _tts(), provider)
    assert provider.calls == 1
    assert seg2.path == seg.path
    assert seg2.duration == 1.0


async def test_hit_reads_from_disk_without_provider(tmp_path):
    cache = TtsCache(tmp_path)
    provider = FakeProvider()
    await cache.get_or_synth("witaj", _tts(), provider)

    # A new provider that would raise if it were ever called
    class Boom:
        adapter_version = 1

        async def synth(self, text, tts, out):  # noqa: ANN001
            raise AssertionError("HIT nie powinien wołać providera")

    seg = await cache.get_or_synth("witaj", _tts(), Boom())
    assert seg.duration == 1.0
    assert seg.path.exists()


async def test_voice_change_is_a_miss(tmp_path):
    cache = TtsCache(tmp_path)
    provider = FakeProvider()

    await cache.get_or_synth("witaj", _tts(voice="A"), provider)
    assert provider.calls == 1

    await cache.get_or_synth("witaj", _tts(voice="B"), provider)
    assert provider.calls == 2  # different voice → different key → MISS


async def test_creates_cache_dir_if_missing(tmp_path):
    target = tmp_path / "nested" / "audio"
    cache = TtsCache(target)
    seg = await cache.get_or_synth("x", _tts(), FakeProvider())
    assert seg.path.exists()
    assert target.is_dir()
