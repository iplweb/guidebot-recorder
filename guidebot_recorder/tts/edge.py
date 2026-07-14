"""Default TTS provider based on edge-tts (no API key).

Synthesis via ``edge_tts.Communicate`` (writes mp3); duration determined by ffprobe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import edge_tts

from guidebot_recorder.models.config import TtsConfig


class EdgeTtsProvider:
    """Microsoft Edge TTS provider (``edge-tts``)."""

    #: bump when synthesis behavior changes while config.tts stays unchanged (§8)
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        """Synthesize ``text`` with voice ``tts.voice`` into the file ``out`` (mp3).

        Returns the duration in seconds (ffprobe). Fail-loud: an empty result → error.
        """
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)

        communicate = edge_tts.Communicate(text, voice=tts.voice)
        await communicate.save(str(out))

        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(
                f"edge-tts nie wygenerował audio dla voice={tts.voice!r} (pusty plik)"
            )

        return await _ffprobe_duration(out)


async def _ffprobe_duration(path: Path) -> float:
    """Audio file duration in seconds via ``ffprobe`` (fail-loud)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe nie odczytał długości {path}: {stderr.decode(errors='replace')}"
        )

    duration = float(json.loads(stdout)["format"]["duration"])
    if duration <= 0:
        raise RuntimeError(f"ffprobe zwrócił niedodatnią długość dla {path}: {duration}")
    return duration
