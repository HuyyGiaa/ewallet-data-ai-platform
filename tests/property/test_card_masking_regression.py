"""Regression suite for B1-F6: payment cards must be masked before any LLM call.

A card is public test data here (``4111111111111111`` and friends). The suite
covers both directions: a valid card is masked whatever surrounds it, and text
the fraud intake depends on is left untouched.
"""

from __future__ import annotations

import re

from hypothesis import given, strategies as st
import pytest

from ai_platform.llm_agent.prompt_safety import _distinct_cards, screen_prompt
from tests.property.test_prompt_safety_properties import (
    invalid_cards,
    luhn_check_digit,
    plain_words,
    valid_cards,
)


MASK = "[REDACTED_PAYMENT_CARD]"
VISA = "4111111111111111"
TEST_CARDS = [
    "4222222222222",  # 13 digits
    "30569309025904",  # 14 digits
    "378282246310005",  # 15 digits
    VISA,
    "5555555555554444",
    "6011111111111117",
    "6011000000000000001",  # 19 digits
]
GAP_SEPARATORS = [" ", "-", "  ", " - ", "\t", " ", "–", "—"]
PUNCTUATION_SEPARATORS = [".", ",", "/", "_"]
GROUP_SEPARATORS = GAP_SEPARATORS + PUNCTUATION_SEPARATORS
NEIGHBOUR_SEPARATORS = [" ", "  ", "-", ".", ",", "/", ":", "_", "\t"]
_ALL_DIGIT_UUID = "12345678-1234-4234-8234-123456789012"
sixteen_digit_cards = valid_cards.filter(lambda card: len(card) == 16)
sixteen_digit_invalid_cards = invalid_cards.filter(lambda number: len(number) == 16)


def grouped(card: str, separator: str, size: int = 4) -> str:
    """Write ``card`` in groups of ``size`` digits."""
    return separator.join(card[i : i + size] for i in range(0, len(card), size))


def digits_of(text: str) -> str:
    """Return the digits of ``text``, so no separator can hide a remaining card."""
    return re.sub(r"\D", "", text)


def assert_masked(text: str, card: str) -> None:
    """Assert the card is gone in every spelling and that it was reported."""
    result = screen_prompt(text)
    assert card not in digits_of(result.text)
    assert result.counts["payment_card"] >= 1
    assert card not in repr(result)


def assert_preserved(text: str) -> None:
    """Assert legitimate text passes through unchanged and is not reported."""
    result = screen_prompt(text)
    assert result.text == text
    assert not result.detected


@pytest.mark.parametrize(
    "text",
    [
        "card 4111111111111111 5 items",
        "the card 4111111111111111 12 times",
        "0000000000018 0@a.com",
    ],
)
def test_reported_b1_f6_inputs_are_masked(text):
    card = next(part for part in text.split() if len(part) >= 13)
    assert_masked(text, card)


def test_the_neighbouring_number_survives_exactly():
    assert screen_prompt("card 4111111111111111 5 items").text == f"card {MASK} 5 items"
    assert screen_prompt("paid 4111111111111111 300 VND").text == f"paid {MASK} 300 VND"
    assert screen_prompt("5 4111111111111111 7").text == f"5 {MASK} 7"
    assert screen_prompt("4111 1111 1111 1111 5").text == f"{MASK} 5"


@pytest.mark.parametrize("card", TEST_CARDS)
@pytest.mark.parametrize("separator", NEIGHBOUR_SEPARATORS)
@pytest.mark.parametrize("number", ["5", "12", "300", "2026", "1000000"])
def test_a_card_next_to_a_number_is_masked(card, separator, number):
    assert_masked(f"{card}{separator}{number}", card)
    assert_masked(f"{number}{separator}{card}", card)


@pytest.mark.parametrize("card", TEST_CARDS)
@pytest.mark.parametrize("separator", GAP_SEPARATORS)
def test_a_card_written_in_groups_is_masked(card, separator):
    assert_masked(f"card {grouped(card, separator)} end", card)


@pytest.mark.parametrize("card", [VISA, "5555555555554444", "6011111111111117"])
@pytest.mark.parametrize("separator", PUNCTUATION_SEPARATORS)
def test_a_card_written_in_punctuated_groups_is_masked(card, separator):
    assert_masked(f"card {grouped(card, separator)} end", card)


@pytest.mark.parametrize("separator", GROUP_SEPARATORS)
def test_an_amex_grouping_is_masked(separator):
    amex = separator.join(["3782", "822463", "10005"])
    assert_masked(f"amex {amex} 7 items", "378282246310005")


@pytest.mark.parametrize(
    "template",
    ["card{}", "thẻ{}", "x-{}", "ref.{}", "card_{}", "#{}", "pan={}", "({})", "{}a1", "{}.", "{}abc", "<{}>"],
)
def test_a_card_attached_to_other_characters_is_masked(template):
    assert_masked(template.format(VISA), VISA)


def test_full_width_digits_do_not_hide_a_card():
    assert screen_prompt("thẻ ４１１１１１１１１１１１１１１１").counts["payment_card"] == 1


def test_adjacent_cards_are_all_masked_and_counted():
    for text in (
        "4111111111111111 5555555555554444",
        "4111 1111 1111 1111 5555 5555 5555 4444",
        "4111-1111-1111-1111 5 5555-5555-5555-4444",
    ):
        result = screen_prompt(text)
        assert result.counts["payment_card"] == 2
        assert not {VISA, "5555555555554444"} & {digits_of(result.text)}
        assert not re.search(r"\d{4}", result.text.replace("-", "").replace(" ", ""))


def test_a_card_touching_an_email_cannot_leave_either_value():
    inside = screen_prompt(f"user{VISA}@example.com")
    assert inside.text == "[REDACTED_EMAIL]"
    assert inside.counts == {"email": 1, "phone": 0, "payment_card": 0}
    assert_masked("4111 1111 1111 1111@example.com", VISA)
    assert screen_prompt(f"{VISA}@example.com").text == "[REDACTED_EMAIL]"


def test_a_card_beside_intake_values_keeps_those_values():
    uuid = "dc58ff4e-b5f7-4486-b1f8-4c31383741a9"
    timestamp = "2026-06-01T00:00:00Z"
    result = screen_prompt(f"{uuid} 4111 1111 1111 1111 {timestamp}")
    assert result.text == f"{uuid} {MASK} {timestamp}"


def test_a_card_glued_to_a_timestamp_is_masked_and_the_timestamp_kept():
    assert screen_prompt(f"2026-06-01T10:20Z{VISA}").text == f"2026-06-01T10:20Z{MASK}"


def test_overlapping_candidates_do_not_hide_a_distinct_card():
    assert _distinct_cards([(0, 19), (1, 14), (15, 30)]) == 2


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1112",  # fails Luhn
        "4111111111111112 5 items",
        "4111-1111-1111-1112",
        "4111.1111.1111.1112",
        "411111111111",  # 12 digits, too short
        "4111 1111 1111",
        "41111111111111115",  # 17 digits fail Luhn; a card is not searched inside
        "4111111111111111" * 3,  # longer than any card
        "ref 1234567890123",
        "amount 1000000 VND",
        "1.000.000.000.000 VND",
        "1,234,567,890,123,456 VND",
        "275465.53 VND",
        "0212345678",
        "2026-06-01",
        "2026-06-01T00:00:00Z",
        "2026-06-01 10:20:30.123456+07:00",
        "12345678 2026-06-01T00:00:00Z",
        _ALL_DIGIT_UUID,
        f"{_ALL_DIGIT_UUID} 5",
        f"5 {_ALL_DIGIT_UUID}",
        "3 items at 2 stores in 12 days",
    ],
)
def test_invalid_candidates_and_legitimate_text_are_preserved(text):
    assert_preserved(text)


@given(plain_words, valid_cards, st.integers(0, 10**6), st.sampled_from(NEIGHBOUR_SEPARATORS))
def test_any_valid_card_next_to_any_number_is_masked(left, card, number, separator):
    assert_masked(f"{left} {card}{separator}{number}", card)
    assert_masked(f"{number}{separator}{card} {left}", card)


@given(valid_cards, st.sampled_from(GAP_SEPARATORS), st.integers(0, 999))
def test_any_grouped_card_next_to_any_number_is_masked(card, separator, number):
    text = grouped(card, separator)
    assert_masked(f"{text} {number} end", card)
    assert_masked(f"start {number} {text}", card)


@given(sixteen_digit_cards, st.sampled_from(PUNCTUATION_SEPARATORS), st.integers(0, 999))
def test_any_punctuated_card_next_to_any_number_is_masked(card, separator, number):
    text = grouped(card, separator)
    assert_masked(f"{text} {number} end", card)
    assert_masked(f"start {number} {text}", card)


@given(sixteen_digit_invalid_cards, st.sampled_from(GROUP_SEPARATORS))
def test_any_grouped_number_failing_the_checksum_is_preserved(number, separator):
    assert_preserved(f"ref {grouped(number, separator)} end")


@given(st.integers(0, 10**18), st.sampled_from([".", ","]))
def test_thousands_grouped_amounts_are_preserved(amount, separator):
    assert_preserved(f"amount {amount:,} VND".replace(",", separator))


@given(st.integers(0, 10**12 - 1), st.integers(0, 99))
def test_decimal_amounts_are_preserved(amount, cents):
    assert_preserved(f"amount {amount}.{cents:02d} VND")


@given(st.text("0123456789", min_size=32, max_size=32), st.integers(0, 10**6))
def test_all_digit_uuids_next_to_numbers_are_preserved(digits, number):
    uuid = f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"
    assert screen_prompt(f"{uuid} {number}").text.startswith(uuid)
    assert screen_prompt(f"{number} {uuid}").text.endswith(uuid)


@given(st.datetimes().map(lambda value: value.isoformat() + "Z"), st.integers(0, 10**12))
def test_timestamps_next_to_numbers_are_preserved(timestamp, number):
    assert timestamp in screen_prompt(f"amount {number} {timestamp}").text


@given(st.lists(st.integers(0, 9), min_size=20, max_size=60))
def test_digits_are_never_searched_inside_a_solid_number(digits):
    number = "".join(map(str, digits))
    number += str(luhn_check_digit(digits))
    assert_preserved(f"ref {number} end")


@given(st.lists(valid_cards | st.integers(0, 999).map(str), max_size=6))
def test_masking_numbers_and_cards_is_idempotent(parts):
    once = screen_prompt(" ".join(parts))
    assert screen_prompt(once.text).text == once.text
