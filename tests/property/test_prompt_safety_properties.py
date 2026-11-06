"""PII masking invariants for text sent to the shared LLM."""

from __future__ import annotations

import string

from hypothesis import given, strategies as st

from ai_platform.llm_agent.prompt_safety import PII_TYPES, screen_prompt


plain_words = st.text(string.ascii_letters + " ", max_size=60)
_digits = st.lists(st.integers(0, 9), min_size=12, max_size=18)
_mobile_prefixes = st.sampled_from(["3", "5", "7", "8", "9"])
_country_prefixes = st.sampled_from(["0", "84", "+84"])


def luhn_check_digit(digits: list[int]) -> int:
    """Return the digit that makes ``digits`` pass the Luhn checksum."""
    total = 0
    for offset, digit in enumerate(reversed(digits)):
        value = digit * 2 if offset % 2 == 0 else digit
        total += value - 9 if value > 9 else value
    return (10 - total % 10) % 10


def _card(digits: list[int], check_digit: int) -> str:
    return "".join(map(str, digits)) + str(check_digit)


valid_cards = _digits.map(lambda digits: _card(digits, luhn_check_digit(digits)))
invalid_cards = st.builds(
    lambda digits, shift: _card(digits, (luhn_check_digit(digits) + shift) % 10),
    _digits,
    st.integers(1, 9),
)
emails = st.builds(
    "{}@{}.{}".format,
    st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=12),
    st.text(string.ascii_lowercase, min_size=1, max_size=10),
    st.sampled_from(["com", "vn", "org"]),
)
phones = st.builds(
    lambda country, prefix, rest: f"{country}{prefix}{rest}",
    _country_prefixes,
    _mobile_prefixes,
    st.text(string.digits, min_size=8, max_size=8),
)


def embed(left: str, secret: str, right: str) -> str:
    """Place ``secret`` between two text fragments as a separate token."""
    return f"{left} {secret} {right}"


@given(plain_words)
def test_text_without_pii_is_unchanged(text):
    result = screen_prompt(text)
    assert result.text == text
    assert not result.detected
    assert result.counts == {name: 0 for name in PII_TYPES}


@given(plain_words, valid_cards, plain_words)
def test_luhn_valid_cards_are_masked(left, card, right):
    result = screen_prompt(embed(left, card, right))
    assert result.text == embed(left, "[REDACTED_PAYMENT_CARD]", right)
    assert result.counts["payment_card"] == 1
    assert result.detected


@given(valid_cards, st.sampled_from([" ", "-"]))
def test_separators_do_not_hide_a_card(card, separator):
    grouped = separator.join(card[i : i + 4] for i in range(0, len(card), 4))
    result = screen_prompt(f"card {grouped} end")
    assert result.counts["payment_card"] == 1
    assert grouped not in result.text


@given(plain_words, invalid_cards, plain_words)
def test_numbers_failing_the_checksum_are_not_cards(left, number, right):
    text = embed(left, number, right)
    result = screen_prompt(text)
    assert result.counts["payment_card"] == 0
    assert number in result.text


@given(plain_words, emails, plain_words)
def test_emails_are_masked(left, email, right):
    result = screen_prompt(embed(left, email, right))
    assert result.text == embed(left, "[REDACTED_EMAIL]", right)
    assert result.counts["email"] == 1


@given(plain_words, phones, plain_words)
def test_vietnamese_mobile_numbers_are_masked(left, phone, right):
    result = screen_prompt(embed(left, phone, right))
    assert result.text == embed(left, "[REDACTED_PHONE]", right)
    assert result.counts["phone"] == 1


@given(
    st.lists(valid_cards, min_size=1, max_size=4),
    st.lists(emails, min_size=1, max_size=4),
    st.lists(phones, min_size=1, max_size=4),
)
def test_every_detection_is_counted(cards, addresses, numbers):
    result = screen_prompt(" and ".join([*cards, *addresses, *numbers]))
    assert result.counts == {
        "email": len(addresses),
        "phone": len(numbers),
        "payment_card": len(cards),
    }


@given(st.uuids().map(str))
def test_uuids_used_by_the_intake_are_preserved(identifier):
    assert screen_prompt(identifier).text == identifier


@given(st.datetimes().map(lambda value: value.isoformat() + "+00:00"))
def test_timestamps_used_by_the_intake_are_preserved(timestamp):
    assert screen_prompt(timestamp).text == timestamp


@given(st.lists(valid_cards | emails | phones | plain_words, max_size=5))
def test_masking_is_idempotent_and_counts_stay_zero_on_masked_text(parts):
    once = screen_prompt(" and ".join(parts))
    twice = screen_prompt(once.text)
    assert twice.text == once.text
    assert not twice.detected


@given(st.text(max_size=120))
def test_screening_arbitrary_text_is_total_and_deterministic(text):
    first = screen_prompt(text)
    assert first == screen_prompt(text)
    assert first.detected is any(first.counts.values())
