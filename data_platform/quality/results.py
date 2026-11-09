"""Result record shared by the Silver, Gold and Phase B quality rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValidationResult:
    rule_name: str
    passed: bool
    actual_value: int | float | str
    expected: str
