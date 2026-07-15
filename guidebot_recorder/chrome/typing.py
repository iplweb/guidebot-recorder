"""Deterministic per-character typing delays for the address-bar animation."""

from __future__ import annotations

import random

# Characters after which a human naturally pauses before typing the next
# segment of a URL (path separators, query delimiters, fragment marker).
_BOUNDARY_CHARS = set("/.?#=&")


def typing_schedule(
    text: str,
    *,
    char_delay_ms: int,
    char_jitter_ms: int,
    segment_pause_ms: int,
    seed: str,
    thinking_pause_ms: int = 500,
    thinking_rate: float = 0.06,
) -> list[int]:
    """Produce one pre-character delay (ms) per character in ``text``.

    The result is fully deterministic for a given ``seed`` and set of
    arguments: all randomness is drawn from a single ``random.Random(seed)``.

    Per character the delay is::

        delay = char_delay_ms
              + uniform(-char_jitter_ms, +char_jitter_ms)
              + (segment_pause_ms if previous char is a boundary else 0)
              + (thinking_pause_ms if random() < thinking_rate else 0)

    then clamped to be a non-negative integer.
    """
    rng = random.Random(seed)
    delays: list[int] = []
    for index, _char in enumerate(text):
        delay = float(char_delay_ms)
        delay += rng.uniform(-char_jitter_ms, char_jitter_ms)
        if index > 0 and text[index - 1] in _BOUNDARY_CHARS:
            delay += segment_pause_ms
        # Always consume the "thinking" draw so the RNG stream is identical
        # regardless of thinking_rate.
        if rng.random() < thinking_rate:
            delay += thinking_pause_ms
        delays.append(max(0, round(delay)))
    return delays
