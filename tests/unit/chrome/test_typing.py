from __future__ import annotations

from guidebot_recorder.chrome.typing import typing_schedule


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
    for base, full in zip(baseline, thinking, strict=True):
        assert full - base == 500
