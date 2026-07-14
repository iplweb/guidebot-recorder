"""Domyślny provider TTS oparty o edge-tts (bez klucza API).

Synteza przez ``edge_tts.Communicate`` (zapis mp3), długość ustalana przez ffprobe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import edge_tts

from guidebot_recorder.models.config import TtsConfig


class EdgeTtsProvider:
    """Provider TTS Microsoft Edge (``edge-tts``)."""

    #: bump przy zmianie zachowania syntezy przy niezmienionym config.tts (§8)
    adapter_version = 1

    async def synth(self, text: str, tts: TtsConfig, out: Path) -> float:
        """Zsyntetyzuj ``text`` głosem ``tts.voice`` do pliku ``out`` (mp3).

        Zwraca długość w sekundach (ffprobe). Fail-loud: pusty wynik → błąd.
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
    """Długość pliku audio w sekundach przez ``ffprobe`` (fail-loud)."""
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
