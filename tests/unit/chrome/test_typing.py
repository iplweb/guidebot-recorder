from __future__ import annotations

import statistics

from guidebot_recorder.chrome.typing import max_typing_delay_ms, typing_schedule


def test_determinism_same_args_equal() -> None:
    kwargs = dict(
        char_delay_ms=100,
        char_jitter_ms=40,
        segment_pause_ms=300,
        seed="abc",
    )
    first = typing_schedule("https://example.com/path?x=1", **kwargs)
    second = typing_schedule("https://example.com/path?x=1", **kwargs)
    assert first == second


def test_different_seed_differs() -> None:
    kwargs = dict(
        char_delay_ms=100,
        char_jitter_ms=40,
        segment_pause_ms=300,
    )
    text = "https://example.com/path?x=1"
    a = typing_schedule(text, seed="seed-one", **kwargs)
    b = typing_schedule(text, seed="seed-two", **kwargs)
    assert a != b


def test_length_matches_text() -> None:
    text = "hello/world.txt"
    result = typing_schedule(
        text,
        char_delay_ms=80,
        char_jitter_ms=20,
        segment_pause_ms=200,
        seed="len",
    )
    assert len(result) == len(text)


def test_empty_string_yields_empty_list() -> None:
    result = typing_schedule(
        "",
        char_delay_ms=80,
        char_jitter_ms=20,
        segment_pause_ms=200,
        seed="empty",
    )
    assert result == []


def test_all_delays_non_negative_ints() -> None:
    result = typing_schedule(
        "a/b.c?d=e&f#g",
        char_delay_ms=10,
        char_jitter_ms=50,
        segment_pause_ms=100,
        seed="ints",
        thinking_rate=0.5,
    )
    assert all(isinstance(d, int) and d >= 0 for d in result)


def test_jitter_only_bound() -> None:
    delay = 100
    jitter = 40
    result = typing_schedule(
        "the-quick-brown-fox-jumps",
        char_delay_ms=delay,
        char_jitter_ms=jitter,
        segment_pause_ms=0,
        seed="jitter",
        thinking_rate=0.0,
    )
    for d in result:
        assert max(0, delay - jitter) <= d <= delay + jitter


def test_boundary_pause_adds_to_next_char() -> None:
    result = typing_schedule(
        "a/b",
        char_delay_ms=100,
        char_jitter_ms=0,
        segment_pause_ms=250,
        seed="boundary",
        thinking_rate=0.0,
    )
    # 'a' and '/' are baseline; 'b' follows the boundary '/'.
    assert result[0] == 100
    assert result[1] == 100
    assert result[2] == 100 + 250


def test_boundary_as_last_char_no_error() -> None:
    result = typing_schedule(
        "abc/",
        char_delay_ms=100,
        char_jitter_ms=0,
        segment_pause_ms=250,
        seed="trailing",
        thinking_rate=0.0,
    )
    assert result == [100, 100, 100, 100]


def test_thinking_rate_one_adds_pause_to_every_char() -> None:
    text = "example.com/some/path"
    common = dict(
        char_delay_ms=90,
        char_jitter_ms=35,
        segment_pause_ms=200,
        seed="think",
        thinking_pause_ms=500,
    )
    baseline = typing_schedule(text, thinking_rate=0.0, **common)
    thinking = typing_schedule(text, thinking_rate=1.0, **common)
    assert len(baseline) == len(thinking) == len(text)
    for index, (base, full) in enumerate(zip(baseline, thinking, strict=True)):
        # A thinking pause never *stacks* on a segment pause: the character right
        # after a boundary already waits, so it is skipped by the thinking draw.
        after_boundary = index > 0 and text[index - 1] in "/.?#=&"
        assert full - base == (0 if after_boundary else 500)


def test_repeated_char_gets_no_segment_pause_and_small_jitter() -> None:
    text = "http://"
    base = 100
    result = typing_schedule(
        text,
        char_delay_ms=base,
        char_jitter_ms=60,
        segment_pause_ms=300,
        seed="url",
        thinking_rate=0.0,
    )
    second_slash = result[text.index("//") + 1]
    # A doubled character is one motor burst: near the base delay, and nothing
    # like the segment pause the *first* separator earns.
    assert abs(second_slash - base) <= 20
    assert second_slash < base + 300 / 2
    # The real boundary (":" is not one, but "/" after ":" is preceded by ":")
    # still pauses: the char right after the second "/" would, were there one.
    first_slash_index = text.index("//")
    assert result[first_slash_index] < base + 300


def test_no_delay_exceeds_ceiling() -> None:
    kwargs = dict(
        char_delay_ms=60,
        char_jitter_ms=55,
        segment_pause_ms=180,
        thinking_pause_ms=500,
        thinking_rate=0.5,
        max_delay_factor=2.5,
    )
    ceiling = max_typing_delay_ms(**kwargs)
    assert ceiling <= 60 * 2.5 + 500
    for seed in (f"seed-{n}" for n in range(50)):
        result = typing_schedule("https://example.com/a//b..c?x=1&y=2#f", seed=seed, **kwargs)
        assert result, "schedule must not be empty"
        assert max(result) <= ceiling


def test_plain_char_delay_capped_by_max_delay_factor() -> None:
    result = typing_schedule(
        "abcdefghijklmnopqrstuvwxyz" * 4,
        char_delay_ms=40,
        char_jitter_ms=10_000,
        segment_pause_ms=0,
        seed="cap",
        thinking_rate=0.0,
        max_delay_factor=2.0,
    )
    assert max(result) <= 80


def test_jitter_is_right_skewed() -> None:
    result = typing_schedule(
        "x" * 0 + "".join(chr(97 + (n * 7) % 26) for n in range(2000)),
        char_delay_ms=100,
        char_jitter_ms=80,
        segment_pause_ms=0,
        seed="skew",
        thinking_rate=0.0,
    )
    # Right-skewed: most characters sit at or below the base delay, with a
    # minority of noticeably slower ones dragging the mean above the median.
    assert statistics.median(result) <= 105
    assert statistics.mean(result) > statistics.median(result)
    assert min(result) >= 100 - 80
