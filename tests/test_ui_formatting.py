from notes.ui.formatting import format_size, format_time, highlight_ranges, parse_rules


def test_utf8_highlight_offsets():
    assert highlight_ranges("中😀A中", "中") == [(0, 3), (8, 3)]
    assert highlight_ranges("你好😀abc", "😀A") == [(6, 5)]


def test_highlight_is_literal_and_case_insensitive():
    assert highlight_ranges("A.*a", ".*") == [(1, 2)]
    assert highlight_ranges("Aa", "a") == [(0, 1), (1, 1)]
    assert highlight_ranges("你好", "") == []
    assert highlight_ranges("İi", "i") == [(0, 2), (2, 1)]


def test_presentation_formatting():
    assert format_size(0) == "0 B"
    assert format_size(2048) == "2.0 KiB"
    assert format_time(0) == "—"
    assert format_time(9999999999999999999999999999) == "—"
    assert parse_rules(".txt, .md，.txt; .log") == (".txt", ".md", ".log")


def test_millisecond_timestamp_contract():
    from datetime import datetime
    epoch_seconds = 1_789_444_800
    expected = datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M")
    for factor in (1, 1000, 1_000_000, 1_000_000_000):
        assert format_time(epoch_seconds * factor) == expected
