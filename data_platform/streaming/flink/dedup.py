"""Bounded transaction-identity deduplication semantics."""

from __future__ import annotations

from datetime import datetime, timedelta


class DedupState:
    def __init__(self, ttl: timedelta) -> None:
        self.ttl = ttl
        self._seen: dict[str, datetime] = {}
        self.input_count = 0
        self.duplicate_count = 0
        self.duplicates_removed = 0
        self.output_unique_count = 0

    def is_duplicate(self, transaction_id: str, observed_at: datetime) -> bool:
        self.input_count += 1
        expired = [key for key, expiry in self._seen.items() if expiry <= observed_at]
        for key in expired:
            del self._seen[key]
        expiry = self._seen.get(transaction_id)
        if expiry is not None and expiry > observed_at:
            self.duplicate_count += 1
            self.duplicates_removed += 1
            return True
        self._seen[transaction_id] = observed_at + self.ttl
        self.output_unique_count += 1
        return False
