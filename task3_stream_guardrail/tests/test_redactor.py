import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redactor import (
    MAX_DIGIT_PATTERN_LENGTH,
    MAX_EMAIL_LENGTH,
    REDACTED,
    StreamRedactor,
    find_pii_spans,
    passes_luhn,
)

R = REDACTED
EMAIL = "john.doe@example.com"
SSN = "123-45-6789"
VISA = "4111 1111 1111 1111"


def with_check_digit(digits: str) -> str:
    for check_digit in "0123456789":
        if passes_luhn(digits + check_digit):
            return digits + check_digit
    raise AssertionError("no check digit found")


LONG_CARD_DIGITS = with_check_digit("123456789012345678")
SEVEN_DIGIT_GROUP_CARD = (
    f"{LONG_CARD_DIGITS[:4]} {LONG_CARD_DIGITS[4:8]} "
    f"{LONG_CARD_DIGITS[8:12]} {LONG_CARD_DIGITS[12:]}"
)


def five_group_card(digits: str, separator: str = " ") -> str:
    groups = [digits[:4], digits[4:8], digits[8:12], digits[12:16], digits[16:]]
    return separator.join(groups)


LONGEST_CARD = five_group_card(LONG_CARD_DIGITS)
VALID_PREFIX_CARD_DIGITS = with_check_digit("411111111111111112")
LONGEST_EMAIL = "L" * 64 + "@" + "d" * 240 + "." + "c" * 24


def every_chunking(text: str):
    yield [text]
    yield list(text)
    for cut in range(1, len(text)):
        yield [text[:cut], text[cut:]]
    generator = random.Random(1107)
    for _ in range(50):
        chunks = []
        position = 0
        while position < len(text):
            size = generator.randint(1, 9)
            chunks.append(text[position:position + size])
            position += size
        yield chunks


def assert_redacts_safely(text: str, expected: str) -> None:
    for chunks in every_chunking(text):
        redactor = StreamRedactor()
        emitted = ""
        for chunk in chunks:
            emitted += redactor.feed(chunk)
            assert expected.startswith(emitted), f"unsafe release for {chunks!r}: {emitted!r}"
        emitted += redactor.flush()
        assert emitted == expected, f"wrong output for {chunks!r}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Contact {EMAIL} for details", f"Contact {R} for details"),
        (f"SSN {SSN} on file", f"SSN {R} on file"),
        ("SSN 123 45 6789 on file", f"SSN {R} on file"),
        (f"Card {VISA} on file", f"Card {R} on file"),
        (f"Mail {EMAIL}.", f"Mail {R}."),
        (f"<{EMAIL}>", f"<{R}>"),
        ("UPPER.CASE+tag@EXAMPLE.CO.UK now", f"{R} now"),
    ],
)
def test_supported_pii_is_redacted_for_every_chunking(text, expected):
    assert_redacts_safely(text, expected)


@pytest.mark.parametrize("value", [EMAIL, SSN, "123 45 6789", VISA, "4111-1111-1111-1111"])
@pytest.mark.parametrize("prefix", ["", "value: ", "\n"])
def test_value_ending_exactly_at_stream_end_is_fully_redacted(prefix, value):
    assert_redacts_safely(prefix + value, prefix + R)


def test_issue_example_split_across_three_chunks():
    redactor = StreamRedactor()
    emitted = [redactor.feed("Contact john@"), redactor.feed("example"), redactor.feed(".com for details")]
    emitted.append(redactor.flush())
    assert "".join(emitted) == f"Contact {R} for details"
    assert "john" not in "".join(emitted[:2])


@pytest.mark.parametrize(
    "card",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111 1111-1111 1111",
        "4222222222222",
        "4222 2222 2222 2",
        "378282246310005",
        "3782 822463 10005",
        "3782-822463-10005",
        "3782 8224 6310 005",
        "3056 930902 5904",
        LONG_CARD_DIGITS,
        SEVEN_DIGIT_GROUP_CARD,
        LONGEST_CARD,
    ],
)
def test_supported_card_formats(card):
    assert passes_luhn(card)
    assert_redacts_safely(f"pay {card} now", f"pay {R} now")


@pytest.mark.parametrize(
    "text",
    [
        "pay 4111 1111 1111 1112 now",
        "pay 4111111111111112 now",
        "pay 3782 822463 10006 now",
        "pay 1234-5678-9012-3456 now",
    ],
)
def test_luhn_invalid_card_numbers_are_preserved(text):
    assert not passes_luhn(text)
    assert_redacts_safely(text, text)


@pytest.mark.parametrize("digits", [LONG_CARD_DIGITS, VALID_PREFIX_CARD_DIGITS])
@pytest.mark.parametrize("separator", [" ", "-"])
def test_five_group_card_becomes_one_marker(digits, separator):
    card = five_group_card(digits, separator)
    assert not passes_luhn(LONG_CARD_DIGITS[:16])
    assert passes_luhn(VALID_PREFIX_CARD_DIGITS[:16])
    assert_redacts_safely(f"pay {card} now", f"pay {R} now")
    assert_redacts_safely(f"pay {card}", f"pay {R}")
    assert_redacts_safely(f"{card}.", f"{R}.")


def test_sixteen_digit_card_keeps_a_following_unrelated_number():
    text = f"{VISA} 124 Main Street"
    assert not passes_luhn(text[:len(VISA) + 4])
    assert_redacts_safely(text, f"{R} 124 Main Street")


def test_five_group_number_failing_luhn_is_preserved():
    card = five_group_card(LONG_CARD_DIGITS[:-1] + str((int(LONG_CARD_DIGITS[-1]) + 1) % 10))
    assert not passes_luhn(card)
    assert_redacts_safely(f"pay {card} now", f"pay {card} now")


@pytest.mark.parametrize("cut", range(16, len(LONGEST_CARD)))
def test_truncated_five_group_card_at_stream_end_exposes_no_group(cut):
    assert_redacts_safely(f"pay {LONGEST_CARD[:cut]}", f"pay {R}")


def test_luhn_check():
    assert passes_luhn(VISA)
    assert passes_luhn("378282246310005")
    assert not passes_luhn("4111111111111112")


@pytest.mark.parametrize(
    "text",
    [
        "Plain prose with no sensitive values at all.",
        "The year 2024 was fine, 42 of 100 passed.",
        "Released on 2024-10-30 at 10:30.",
        "Call 555-123-4567 today.",
        "Order number 1234 5678 stays visible.",
        "Ratio 1-2-3 and range 10 - 20 stay.",
        "Handle @someone and a@b are not emails here.",
        "hello.world-name_x+y is one token.",
        "version 1.2.3-beta+build ok",
        "1234-56-789 is the wrong shape.",
        "123-45-67890 has too many digits.",
        "0123-45-6789 has too many digits.",
        "123-45 6789 mixes separators.",
        "123456789012 has too few digits.",
        "12345678901234567890 has too many digits.",
        "\ttabs\nand\r\nnewlines   stay\n\n",
    ],
)
def test_ordinary_text_is_preserved(text):
    assert_redacts_safely(text, text)


def test_multiple_values_in_one_response():
    text = f"Mail {EMAIL}, SSN {SSN}; card {VISA}. Backup anna@example.org"
    assert_redacts_safely(text, f"Mail {R}, SSN {R}; card {R}. Backup {R}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a@b.com,c@d.org", f"{R},{R}"),
        ("a@b.com c@d.org", f"{R} {R}"),
        ("a@b.com+c@d.org", f"{R}{R}"),
        (f"{SSN} {SSN} {SSN} end", f"{R} {R} {R} end"),
        (f"{VISA} {VISA} end", f"{R} {R} end"),
        (f"{SSN} {VISA} a@b.com", f"{R} {R} {R}"),
        (f"{VISA} {SSN} {VISA}", f"{R} {R} {R}"),
        (f"{SSN}{EMAIL} end", f"{R} end"),
        (f"{VISA}x", f"{R}x"),
        (f"{SSN}x", f"{R}x"),
        (f"1234 {VISA} end", f"1234 {R} end"),
        (f"12 34 56 78 {VISA} end", f"12 34 56 78 {R} end"),
        (f"{VISA} 123 end", f"{R} 123 end"),
        (f"{VISA}-john@example.com end", f"{R}{R} end"),
        ("4111-1111@example.com end", f"{R} end"),
        ("123-45-6789@example.com end", f"{R} end"),
        (f"{SSN}-john@example.com end", f"{R} end"),
    ],
)
def test_adjacent_and_overlapping_candidates(text, expected):
    assert_redacts_safely(text, expected)


def test_failed_luhn_candidate_does_not_hide_following_card():
    text = f"x 1234 {VISA}"
    spans = find_pii_spans(text)
    assert [text[start:stop] for start, stop in spans] == [VISA]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail john@", f"mail {R}"),
        ("mail john@exam", f"mail {R}"),
        ("mail john@example.", f"mail {R}"),
        ("mail john@example.c", f"mail {R}"),
        ("ssn 123-45-", f"ssn {R}"),
        ("ssn 123-45-6", f"ssn {R}"),
        ("ssn 123 45 678", f"ssn {R}"),
        ("card 4111 1111 ", f"card {R}"),
        ("card 4111 1111 1", f"card {R}"),
        ("card 4111 1111 1111 1", f"card {R}"),
        ("card 4111-1111-1111-111", f"card {R}"),
        ("card 411111111111", f"card {R}"),
        ("card 411111111111111", f"card {R}"),
        ("amex 3782 822463 ", f"amex {R}"),
        ("amex 3782 822463 1", f"amex {R}"),
        ("short @", "short @"),
        ("short 4111", "short 4111"),
        ("short 123-4", "short 123-4"),
        ("short 4111 1111", "short 4111 1111"),
        ("ends with 2024", "ends with 2024"),
    ],
)
def test_incomplete_values_at_stream_end(text, expected):
    assert_redacts_safely(text, expected)


def test_luhn_invalid_card_at_stream_end_is_over_redacted():
    assert_redacts_safely("pay 1234 5678 9012 3456", f"pay {R}")


def test_incomplete_value_followed_by_ordinary_text_is_released():
    assert_redacts_safely("john@exam ple and 123-45 more", "john@exam ple and 123-45 more")


def test_cap_constants_match_longest_supported_values():
    assert len(LONGEST_CARD) == MAX_DIGIT_PATTERN_LENGTH
    assert len(LONGEST_EMAIL) == MAX_EMAIL_LENGTH
    assert find_pii_spans(LONGEST_CARD) == [(0, MAX_DIGIT_PATTERN_LENGTH)]
    assert find_pii_spans(LONGEST_EMAIL) == [(0, MAX_EMAIL_LENGTH)]


@pytest.mark.parametrize("filler_pairs", range(8, 16))
@pytest.mark.parametrize("value", [VISA, LONGEST_CARD, SSN])
def test_digit_value_after_filler_around_the_22_character_cap(filler_pairs, value):
    filler = "7 " * filler_pairs
    assert_redacts_safely(f"{filler}{value} end", f"{filler}{R} end")
    assert_redacts_safely(f"{filler}{value}", f"{filler}{R}")


@pytest.mark.parametrize("trailing_spaces", range(0, 8))
def test_card_crossing_the_digit_cap_is_held_back_whole(trailing_spaces):
    spaces = " " * trailing_spaces
    assert_redacts_safely(f"{VISA}{spaces}x", f"{R}{spaces}x")
    assert_redacts_safely(f"{SSN} {VISA}{spaces}x", f"{R} {R}{spaces}x")


@pytest.mark.parametrize("filler_length", [0, 8, 9, 10, 11, 12, 40])
def test_longest_email_around_the_330_character_cap(filler_length):
    filler = "x" * filler_length + " " if filler_length else ""
    assert_redacts_safely(f"{filler}{LONGEST_EMAIL} end", f"{filler}{R} end")
    assert_redacts_safely(f"{filler}{LONGEST_EMAIL}", f"{filler}{R}")


@pytest.mark.parametrize("run_length", [329, 330, 331, 332, 700])
def test_email_after_a_long_token_around_the_330_character_cap(run_length):
    run = "x" * run_length
    assert_redacts_safely(f"{run} a@b.com end", f"{run} {R} end")


@pytest.mark.parametrize("run_length", [329, 330, 331, 400])
def test_email_inside_a_long_token_redacts_up_to_the_local_part_limit(run_length):
    run = "x" * run_length
    assert_redacts_safely(f"{run}a@b.com end", f"{'x' * (run_length - 63)}{R} end")


@pytest.mark.parametrize(
    ("unit", "limit"),
    [
        ("a", MAX_EMAIL_LENGTH),
        ("9", MAX_EMAIL_LENGTH),
        ("a.", MAX_EMAIL_LENGTH),
        ("a@", MAX_EMAIL_LENGTH),
        ("7 ", MAX_DIGIT_PATTERN_LENGTH),
        ("7-", MAX_EMAIL_LENGTH),
        ("word ", len("word")),
        (" ", 0),
        ("日本語", 0),
    ],
)
def test_held_buffer_stays_bounded_on_pathological_input(unit, limit):
    redactor = StreamRedactor()
    largest = 0
    for _ in range(3000):
        redactor.feed(unit)
        largest = max(largest, len(redactor._buffer))
    assert largest <= limit


def test_held_buffer_never_exceeds_twice_the_email_cap():
    text = f"{VISA} {SSN} a@b.com " * 200 + "7 " * 500 + LONGEST_EMAIL + "-" * 500
    redactor = StreamRedactor()
    for character in text:
        redactor.feed(character)
        assert len(redactor._buffer) <= 2 * MAX_EMAIL_LENGTH


def test_long_ordinary_text_streams_with_small_buffer():
    sentence = "The quick brown fox jumps over the lazy dog. "
    text = sentence * 2500
    redactor = StreamRedactor()
    emitted = 0
    for position in range(0, len(text), 7):
        emitted += len(redactor.feed(text[position:position + 7]))
        assert len(redactor._buffer) <= len("jumps") + 7
    assert emitted + len(redactor.flush()) == len(text)


def test_long_text_with_sprinkled_pii():
    block = "plain words here " * 20
    text = f"{block}{EMAIL} {block}{VISA} {block}{SSN} " * 50
    expected = f"{block}{R} {block}{R} {block}{R} " * 50
    redactor = StreamRedactor()
    pieces = [redactor.feed(text[position:position + 11]) for position in range(0, len(text), 11)]
    assert "".join(pieces) + redactor.flush() == expected


def test_redaction_throughput_is_not_a_bottleneck():
    text = "The quick brown fox jumps over the lazy dog. " * 24000
    redactor = StreamRedactor()
    start = time.perf_counter()
    for position in range(0, len(text), 16):
        redactor.feed(text[position:position + 16])
    redactor.flush()
    assert time.perf_counter() - start < 5


@pytest.mark.parametrize(
    "text",
    [
        "café ☕ naïve résumé",
        "日本語のテキスト",
        "emoji 😀👍🏽 mix",
        "Ωmega — “quotes” … €100",
    ],
)
def test_unicode_text_is_preserved(text):
    assert_redacts_safely(text, text)


def test_pii_inside_unicode_text_is_redacted():
    assert_redacts_safely(f"連絡先{EMAIL}です ☕ {SSN} café", f"連絡先{R}です ☕ {R} café")


def test_text_without_pii_candidates_is_released_immediately():
    assert StreamRedactor().feed("日本語 ☕ ") == "日本語 ☕ "
    assert StreamRedactor().feed("hello world ") == "hello world "


def test_only_the_word_in_progress_is_held_back():
    redactor = StreamRedactor()
    assert redactor.feed("Contact john") == "Contact "
    assert redactor.feed(" now") == "john "
    assert redactor.flush() == "now"


def test_digit_run_is_held_until_it_cannot_be_pii():
    redactor = StreamRedactor()
    assert redactor.feed("Total: 1234 ") == "Total: "
    assert redactor.feed("items ") == "1234 items "


def test_complete_email_is_held_while_it_could_still_extend():
    redactor = StreamRedactor()
    assert redactor.feed("a@example.co") == ""
    assert redactor.feed("m.au ok") == f"{R} "
    assert redactor.flush() == "ok"


def test_flush_releases_held_text_and_empties_the_redactor():
    redactor = StreamRedactor()
    assert redactor.feed("hello") == ""
    assert redactor.flush() == "hello"
    assert redactor.flush() == ""


def test_flush_on_empty_redactor():
    assert StreamRedactor().flush() == ""


def test_empty_feeds_do_not_change_output():
    redactor = StreamRedactor()
    assert redactor.feed("") == ""
    assert redactor.feed("a@b") == ""
    assert redactor.feed("") == ""
    assert redactor.feed(".com ") == f"{R} "


def test_redactor_is_reusable_after_flush():
    redactor = StreamRedactor()
    assert redactor.feed("first a@b.com") == "first "
    assert redactor.flush() == R
    assert redactor.feed("second ") == "second "
