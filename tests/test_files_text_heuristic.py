"""Exact semantic conformance for the legacy whole-text binary heuristic.

These tests contain no timing thresholds. They are prepared before changing
the production helper so the benchmark can retain an untouched source baseline.
"""

import random

import pytest

from notes.infrastructure import files


def old_expression(text):
    """Independent literal copy of the expression used before optimization."""
    return "\x00" in text or bool(
        text and sum(ord(character) < 32 and character not in "\t\n\r\f"
                     for character in text) / len(text) > 0.02
    )


@pytest.fixture
def subject():
    # Resolve only when executing: test collection does not require the helper
    # to exist while the original production source is frozen for benchmarking.
    return files._looks_binary


def test_empty_and_each_byte_value_as_unicode_codepoint(subject):
    assert subject("") is False
    for value in range(256):
        character = chr(value)
        for text in (character, character * 100, "x" * 49 + character,
                     "x" * 48 + character, "x" * 50 + character):
            assert subject(text) is old_expression(text), (value, len(text))


@pytest.mark.parametrize("control", ["\t", "\n", "\r", "\f"])
def test_four_excluded_c0_characters_are_text_even_at_full_density(subject, control):
    for text in (control, control * 5000, "中文😀" + control * 1000):
        assert old_expression(text) is False
        assert subject(text) is False


@pytest.mark.parametrize("character", [
    "\x7f", "\x80", "\x85", "\x9f", "\u2028", "\u2029", "\u200d",
    "\ufeff", "\u202e", "\u0301", "中", "é", "😀", "\U0010ffff", "\ud800", "\udfff",
], ids=[
    "del", "c1-start", "c1-nel", "c1-end", "line-separator", "paragraph-separator",
    "zwj", "bom-codepoint", "bidi", "combining", "cjk", "accent", "emoji",
    "max-codepoint", "high-surrogate", "low-surrogate",
])
def test_del_c1_and_unicode_are_not_newly_classified_as_binary(subject, character):
    text = character * 1000
    assert old_expression(text) is False
    assert subject(text) is False


@pytest.mark.parametrize("length,controls", [
    (1, 0), (1, 1), (49, 1), (50, 1), (51, 1),
    (99, 2), (100, 2), (100, 3), (101, 2), (101, 3),
    (4999, 99), (5000, 100), (5000, 101),
    (5001, 100), (5001, 101),
])
@pytest.mark.parametrize("layout", ["prefix", "suffix", "interleaved"])
def test_strict_two_percent_boundary_and_layout_do_not_change_meaning(
    subject, length, controls, layout
):
    if layout == "prefix":
        text = "\x01" * controls + "中" * (length - controls)
    elif layout == "suffix":
        text = "😀" * (length - controls) + "\x0b" * controls
    else:
        parts = ["é"] * length
        if controls:
            for index in range(controls):
                parts[index * length // controls] = "\x1f"
        text = "".join(parts)
    assert len(text) == length
    assert subject(text) is old_expression(text), (length, controls, layout)
    if length in (50, 100, 5000) and controls == length // 50:
        assert subject(text) is False


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_one_nul_is_binary_independent_of_density_or_position(subject, position):
    text = "clean中文😀\t\n\r\f" * 20_000
    offset = {"first": 0, "middle": len(text) // 2, "last": len(text)}[position]
    text = text[:offset] + "\x00" + text[offset:]
    assert old_expression(text) is True
    assert subject(text) is True


def test_counts_unicode_characters_not_utf8_bytes(subject):
    binary = "😀" * 48 + "\x01"
    text = "😀" * 49 + "\x01"
    assert len(binary.encode("utf-8")) > len(binary)
    assert old_expression(binary) is True and subject(binary) is True
    assert old_expression(text) is False and subject(text) is False


def test_fixed_seed_mixed_unicode_and_thresholds_match_literal_old_expression(subject):
    randomizer = random.Random(20260917)
    allowed = "abcXYZ中文éÉ\u0301\u200d😀\U0010ffff\ud800\udfff\t\n\r\f\x7f\x80\x85\x9f"
    bad = "".join(chr(value) for value in range(1, 32) if chr(value) not in "\t\n\r\f")
    for case in range(150):
        length = randomizer.randrange(0, 4097)
        proportion = randomizer.choice((0, 0.005, 0.019, 0.02, 0.021, 0.5, 1))
        controls = int(length * proportion)
        characters = [randomizer.choice(allowed) for _ in range(length)]
        for position in randomizer.sample(range(length), controls):
            characters[position] = randomizer.choice(bad)
        with_nul = bool(length and case % 13 == 0)
        if with_nul:
            characters[randomizer.randrange(length)] = "\x00"
        text = "".join(characters)
        assert subject(text) is old_expression(text), (case, length, controls, with_nul)


@pytest.mark.parametrize("length,controls", [
    (5_000_000, 100_000),
    (5_000_000, 100_001),
    (5 * 1024 * 1024, 104_857),
    (5 * 1024 * 1024, 104_858),
], ids=["large-exact-two-percent", "large-just-over", "max-size-below", "max-size-above"])
def test_long_clean_prefix_cannot_hide_control_characters_at_the_end(subject, length, controls):
    # All controls are beyond any tempting prefix sample. These cases remain
    # within the current full-read byte limit; no performance assertion is made.
    text = "x" * (length - controls) + "\x01" * controls
    assert length <= files.INDEX_LIMIT
    assert subject(text) is old_expression(text), (length, controls)


@pytest.mark.parametrize("text,expected", [
    ("😀" * 49 + "\x01", "ready"),
    ("😀" * 48 + "\x01", "skipped_binary"),
    ("中文\x85\u2028\u200d😀\n", "ready"),
    ("long clean prefix" * 1000 + "\x00", "skipped_binary"),
], ids=["two-percent", "over-two-percent", "unicode-controls-not-c0", "late-nul"])
def test_read_document_keeps_existing_decoding_and_status_contract(tmp_path, text, expected):
    path = tmp_path / "sample.txt"
    original = text.encode("utf-8")
    path.write_bytes(original)
    result = files.read_document(path)
    assert result.status == expected
    assert path.read_bytes() == original
    if expected == "ready":
        assert result.text == text
