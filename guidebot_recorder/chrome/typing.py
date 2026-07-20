"""Deterministic per-character typing delays for the address-bar animation."""

from __future__ import annotations

import math
import random

# Characters after which a human naturally pauses before typing the next
# segment of a URL (path separators, query delimiters, fragment marker).
_BOUNDARY_CHARS = set("/.?#=&")

# A doubled character ("//", "..", "aa") is one motor burst, not two decisions:
# the second keystroke keeps only a fraction of the normal jitter.
_REPEAT_JITTER_SCALE = 0.2

# How much of the jitter/base ratio becomes the log-normal sigma. Keeps the
# distribution recognisably skewed without exploding into the clamp band.
_SIGMA_SCALE = 0.5
_SIGMA_MAX = 0.45

DEFAULT_MAX_DELAY_FACTOR = 2.5


def max_typing_delay_ms(
    *,
    char_delay_ms: int,
    char_jitter_ms: int,
    segment_pause_ms: int,
    thinking_pause_ms: int = 500,
    thinking_rate: float = 0.06,
    max_delay_factor: float = DEFAULT_MAX_DELAY_FACTOR,
) -> int:
    """The hard upper bound :func:`typing_schedule` can ever emit, in ms.

    A single character never waits longer than ``char_delay_ms *
    max_delay_factor``; on top of that it may earn *either* a segment pause
    (real URL boundary) *or* a thinking pause — never both — so the deliberate
    pause is itself bounded and can no longer stack into an absurd outlier.
    """
    del char_jitter_ms  # jitter is clamped inside the ceiling below
    extra = max(segment_pause_ms, thinking_pause_ms if thinking_rate > 0 else 0)
    return max(0, round(char_delay_ms * max_delay_factor + extra))


def _skewed_char_delay(rng: random.Random, base: float, jitter: float) -> float:
    """One right-skewed per-character delay around ``base``.

    Human keystroke intervals are not symmetric: there is a floor you cannot
    type faster than, and a long right tail of occasional slower characters.
    A log-normal draw (median ``base``) reproduces that shape; the result is
    clamped into ``base ± jitter`` so the classic jitter bound still holds.
    """
    if base <= 0 or jitter <= 0:
        return base
    sigma = min(_SIGMA_MAX, _SIGMA_SCALE * jitter / base)
    value = base * math.exp(rng.gauss(0.0, sigma))
    return min(max(value, base - jitter), base + jitter)


def typing_schedule(
    text: str,
    *,
    char_delay_ms: int,
    char_jitter_ms: int,
    segment_pause_ms: int,
    seed: str,
    thinking_pause_ms: int = 500,
    thinking_rate: float = 0.06,
    max_delay_factor: float = DEFAULT_MAX_DELAY_FACTOR,
) -> list[int]:
    """Produce one pre-character delay (ms) per character in ``text``.

    The result is fully deterministic for a given ``seed`` and set of
    arguments: all randomness is drawn from a single ``random.Random(seed)``.

    The model is contextual rather than uniform:

    * the base delay is drawn from a right-skewed (log-normal) distribution
      around ``char_delay_ms``, clamped into ``± char_jitter_ms`` and capped at
      ``char_delay_ms * max_delay_factor``;
    * a character repeating the previous one ("//", "..", "aa") is typed as a
      single motor burst — a fraction of the jitter and *no* segment pause,
      which is what used to tear "://" apart;
    * ``segment_pause_ms`` still fires at real segment boundaries (after
      "http:", after a "."), i.e. when the previous character is a boundary
      character and is not simply repeated;
    * the ``thinking_pause_ms`` beat fires with probability ``thinking_rate``
      but never on a character that already waits (post-boundary or repeated),
      so the pauses cannot stack into an absurd outlier.
    """
    rng = random.Random(seed)
    base = float(char_delay_ms)
    ceiling = base * max_delay_factor
    delays: list[int] = []
    for index, char in enumerate(text):
        previous = text[index - 1] if index > 0 else None
        repeated = previous is not None and previous == char
        jitter = float(char_jitter_ms) * (_REPEAT_JITTER_SCALE if repeated else 1.0)
        delay = min(_skewed_char_delay(rng, base, jitter), ceiling)
        at_boundary = previous is not None and previous in _BOUNDARY_CHARS and not repeated
        if at_boundary:
            delay += segment_pause_ms
        # Always consume the "thinking" draw so the RNG stream is identical
        # regardless of thinking_rate.
        thinking = rng.random() < thinking_rate
        if thinking and not at_boundary and not repeated:
            delay += thinking_pause_ms
        delays.append(max(0, round(delay)))
    return delays
