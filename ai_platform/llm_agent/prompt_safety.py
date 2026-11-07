"""Privacy-preserving PII screen for prompts sent to the shared LLM.

Applied by the bounded natural-language intake before any LLM call. Detected
values are replaced with typed placeholders, so they never reach agentgateway,
vLLM, traces or logs. Only counts and types are reported; the matched values are
never returned, logged or exported.

Detectors are deliberately conservative and deterministic:

- email addresses;
- Vietnamese mobile numbers (+84 / 84 / 0 followed by a 3/5/7/8/9 prefix and 8 digits);
- payment card numbers (13–19 digits with a valid Luhn checksum).

A card is judged on its own digits, never together with a neighbouring number,
so ``card 4111111111111111 5 items`` is still masked:

1. a solid run of 13–19 digits that passes Luhn is masked first, whatever
   surrounds it;
2. in what remains, digit groups joined by whitespace or dashes, or by
   ``. , / _`` between groups of four or more digits, are tested as every
   consecutive sequence of groups. All valid sequences are masked, so a
   coincidental overlap can only mask too much, never too little.

Digits are never searched inside a solid run: any window of a long number passes
Luhn one time in ten. UUIDs and ISO timestamps used by the fraud intake are left
intact, thousands-grouped and decimal amounts cannot form a card, and digits
inside an email address are left for the email detector.

Phones and emails are likewise judged on their own characters: only a digit
directly attached to a phone, or a letter directly attached to an email's
top-level domain, stops a match. ``0912345678-5``, ``sdt0912345678`` and
``a@b.com-5`` are all masked.

Before detection the text is put in a canonical form, so look-alike characters
cannot hide a value: invisible format characters (zero-width spaces, joiners,
soft hyphens) are removed, and full-width ASCII and decimal digits of any script
become their ASCII equivalents. The canonical text is what the LLM receives.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import re
from typing import Literal
import unicodedata

PiiType = Literal["email", "phone", "payment_card"]
PII_TYPES: tuple[PiiType, ...] = ("email", "phone", "payment_card")

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z])"
)
# Horizontal whitespace, dots and dash variants may separate any two phone digits.
_PHONE_GAP = r"[ \t  -  　.\-‐-―−]"
_PHONE = re.compile(
    rf"(?<![\d+])(?:\(?(?:\+{_PHONE_GAP}?)?8{_PHONE_GAP}?4\)?|0){_PHONE_GAP}?[35789](?:{_PHONE_GAP}?\d){{8}}(?!\d)"
)
_INVISIBLE = frozenset("­᠎​‌‍⁠⁡⁢⁣⁤﻿")

_CARD_MASK = "[REDACTED_PAYMENT_CARD]"
_MIN_CARD_DIGITS, _MAX_CARD_DIGITS = 13, 19
_DIGIT_GROUP = re.compile(r"\d+")
_SOLID_NUMBER = re.compile(rf"(?<!\d)\d{{{_MIN_CARD_DIGITS},{_MAX_CARD_DIGITS}}}(?!\d)")
# Horizontal whitespace and dash variants; a line break never joins two numbers.
_GROUP_GAP = r"[ \t\u00a0\u2000-\u200a\u202f\u3000\-\u2010-\u2015\u2212]{1,3}"
# Punctuation joins only groups of 4+ digits, which 1.000.000 and 275465.53 are not.
_DIGIT_RUN = re.compile(rf"\d+(?:{_GROUP_GAP}\d+|(?<=\d{{4}})[.,/_]\d{{4,}})*")
# Same UUID shape as nl_intake, which imports this module and so cannot share it.
_INTAKE_VALUE = re.compile(
    r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F-])"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _canonical_char(char: str) -> str:
    """Return the ASCII form of a look-alike character, or nothing for an invisible one."""

    if char in _INVISIBLE:
        return ""
    if "！" <= char <= "～":
        return chr(ord(char) - 0xFEE0)
    digit = unicodedata.decimal(char, None)
    return str(digit) if digit is not None else char


def canonical_text(text: str) -> str:
    """Remove invisible characters and map full-width ASCII and decimal digits to ASCII."""

    if text.isascii():
        return text
    return "".join(char if char.isascii() else _canonical_char(char) for char in text)


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for char in reversed(digits):
        value = int(char)
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def _is_card(digits: str) -> bool:
    return _MIN_CARD_DIGITS <= len(digits) <= _MAX_CARD_DIGITS and _luhn(digits)


def _grouped_card_spans(run: str) -> Iterator[tuple[int, int]]:
    """Yield the offsets of each valid card made of consecutive groups in ``run``."""

    groups = [group.span() for group in _DIGIT_GROUP.finditer(run)]
    for index, (start, _) in enumerate(groups):
        digits = ""
        for group_start, group_end in groups[index:index + _MAX_CARD_DIGITS]:
            digits += run[group_start:group_end]
            if _is_card(digits):
                yield start, group_end


def _distinct_cards(spans: Iterable[tuple[int, int]]) -> int:
    """Count non-overlapping spans, so a coincidental overlap does not hide a card."""

    cards, covered_until = 0, -1
    for start, end in sorted(spans, key=lambda span: span[1]):
        if start >= covered_until:
            cards, covered_until = cards + 1, end
    return cards


def _mask_spans(text: str, spans: Iterable[tuple[int, int]]) -> tuple[str, int]:
    """Mask ``spans``, merging overlaps, and return the text and the cards found.

    A span is skipped when it touches a UUID or timestamp, or lies inside an
    email address, which the email detector then masks as a whole.
    """

    intake_values = [match.span() for match in _INTAKE_VALUE.finditer(text)]
    emails = [match.span() for match in _EMAIL.finditer(text)]

    def maskable(start: int, end: int) -> bool:
        return not any(start < value_end and value_start < end for value_start, value_end in intake_values) and not any(
            email_start <= start and end <= email_end for email_start, email_end in emails
        )

    kept = sorted(span for span in spans if maskable(*span))
    merged: list[list[int]] = []
    for start, end in kept:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = f"{text[:start]}{_CARD_MASK}{text[end:]}"
    return text, _distinct_cards(kept)


def _mask_cards(text: str) -> tuple[str, int]:
    """Mask payment cards and return the masked text and the number of cards."""

    text, solid = _mask_spans(
        text, [match.span() for match in _SOLID_NUMBER.finditer(text) if _is_card(match.group(0))]
    )
    text, grouped = _mask_spans(
        text,
        [
            (run.start() + start, run.start() + end)
            for run in _DIGIT_RUN.finditer(text)
            for start, end in _grouped_card_spans(run.group(0))
        ],
    )
    return text, solid + grouped


@dataclass(frozen=True)
class ScreenResult:
    """Masked text and per-type counts; never the detected values."""

    text: str
    counts: dict[str, int]

    @property
    def detected(self) -> bool:
        """Whether any PII type was found."""

        return any(self.counts.values())


def screen_prompt(text: str) -> ScreenResult:
    """Mask PII in a prompt and return only aggregate detection counts."""

    counts = {name: 0 for name in PII_TYPES}

    def email(_match: re.Match[str]) -> str:
        counts["email"] += 1
        return "[REDACTED_EMAIL]"

    masked, counts["payment_card"] = _mask_cards(canonical_text(text))
    masked = _EMAIL.sub(email, masked)
    masked, counts["phone"] = _mask_phones(masked)
    return ScreenResult(text=masked, counts=counts)


def _mask_phones(text: str) -> tuple[str, int]:
    """Mask phone numbers, never inside a UUID or timestamp the intake must ground."""

    intake_values = [match.span() for match in _INTAKE_VALUE.finditer(text)]
    phones = [
        match.span()
        for match in _PHONE.finditer(text)
        if not any(match.start() < end and start < match.end() for start, end in intake_values)
    ]
    for start, end in reversed(phones):
        text = f"{text[:start]}[REDACTED_PHONE]{text[end:]}"
    return text, len(phones)
