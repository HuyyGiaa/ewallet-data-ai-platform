"""Deterministic chronological split policy derived from canonical F4C data."""

from __future__ import annotations

import pandas as pd


# The original audit displayed these instants in Asia/Ho_Chi_Minh. Persisted
# Delta and Feast timestamps are UTC, so the executable boundaries use the
# equivalent UTC instants rather than relabelling local wall time as UTC.
VALIDATION_START = pd.Timestamp("2026-04-16 12:14:11.022705", tz="UTC")
TEST_START = pd.Timestamp("2026-05-09 02:38:44.058669", tz="UTC")
SPLIT_ORDER = ("train", "validation", "test")


def assign_temporal_split(timestamps: pd.Series) -> pd.Series:
    values = pd.to_datetime(timestamps, utc=True)
    result = pd.Series("test", index=values.index, dtype="string")
    result.loc[values < TEST_START] = "validation"
    result.loc[values < VALIDATION_START] = "train"
    return result


def validate_split_boundaries() -> None:
    if not VALIDATION_START < TEST_START:
        raise ValueError("Temporal split boundaries are not chronological")


validate_split_boundaries()
