"""Test EdgeTtsProvider (Task 15) — requires network, opt-in (@pytest.mark.network)."""

from __future__ import annotations

import pytest

from guidebot_recorder.models.config import TtsConfig
from guidebot_recorder.tts.edge import EdgeTtsProvider


@pytest.mark.network
async def test_edge_synth_generates_nonempty_audio(tmp_path):
    provider = EdgeTtsProvider()
    tts = TtsConfig(provider="edge", voice="pl-PL-ZofiaNeural", lang="pl-PL")
    out = tmp_path / "seg.mp3"

    duration = await provider.synth("Dzień dobry, to jest test.", tts, out)

    assert out.exists()
    assert out.stat().st_size > 0
    assert duration > 0
