"""Privacy-preserving PII screen for prompts sent to the shared LLM.

Applied by the bounded natural-language intake before any LLM call. Detected
values are replaced with typed placeholders, so they never reach agentgateway,
vLLM, traces or logs. Only counts and types are reported; the matched values are
never returned, logged or exported.

Detectors are deliberately conservative and deterministic:

- email addresses;
- Vietnamese mobile numbers (+84 / 84 / 0 followed by a 3/5/7/8/9 prefix and 8 digits);
- payment card numbers (13–19 digits, separators allowed, Luhn checksum).

UUIDs, ISO timestamps and decimal amounts used by the fraud intake are not
matched: UUID digit runs are hex-bounded, amounts carry no card checksum and do
not have a mobile prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

PiiType = Literal["email", "phone", "payment_card"]
PII_TYPES: tuple[PiiType, ...] = ("email", "phone", "payment_card")

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_PHONE = re.compile(r"(?<![\w+.-])(?:\+84|84|0)[ .-]?(?:3|5|7|8|9)\d(?:[ .-]?\d){7}(?![\w.-]*\d)")
_CARD = re.compile(r"(?<![\w.-])\d(?:[ -]?\d){12,18}(?![\w.-]*\d)")


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


@dataclass(frozen=True)
class ScreenResult:
    """Masked text and per-type counts; never the detected values."""

    text: str
    counts: dict[str, int]

    @property
    def detected(self) -> bool:
        return any(self.counts.values())


def screen_prompt(text: str) -> ScreenResult:
    """Mask PII in a prompt and return only aggregate detection counts."""

    counts = {name: 0 for name in PII_TYPES}

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            counts["payment_card"] += 1
            return "[REDACTED_PAYMENT_CARD]"
        return match.group(0)

    def email(_match: re.Match[str]) -> str:
        counts["email"] += 1
        return "[REDACTED_EMAIL]"

    def phone(_match: re.Match[str]) -> str:
        counts["phone"] += 1
        return "[REDACTED_PHONE]"

    masked = _CARD.sub(card, text)
    masked = _EMAIL.sub(email, masked)
    masked = _PHONE.sub(phone, masked)
    return ScreenResult(text=masked, counts=counts)
