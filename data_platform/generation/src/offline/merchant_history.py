"""Synthetic merchant attribute history for the SCD Type 2 merchant dimension.

The legacy ``merchants`` table holds one current state per merchant and no
effective time, so it cannot establish history. This module emits an explicit,
persisted source table of full merchant snapshots:

* one snapshot per merchant at each scheduled snapshot time, starting at the
  first transaction-time instant (``generation.temporal.start_timestamp``) so
  every transaction falls inside a recorded state;
* most merchants never change; a configured share change their name and/or
  category once or twice, so later snapshots differ from earlier ones;
* unchanged snapshots repeat the previous state, which the SCD2 builder must
  collapse into one version;
* the last snapshot equals the merchant's row in ``merchants``.

The history is synthetic. Every row carries ``history_source = "synthetic"`` and
the dataset manifest records the generator, so it is never mistaken for observed
merchant history.

Randomness comes from a private ``random.Random`` and a private ``Faker``
instance seeded from the shard seed. The module never touches the global
``random``, ``numpy`` or ``Faker`` state, so enabling it leaves the nine legacy
tables of the same seed byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

import pandas as pd
from faker import Faker

from data_platform.generation.src.fintech_schema import MerchantCategory


MERCHANT_HISTORY_TABLE = "merchant_history"
HISTORY_SOURCE = "synthetic"
HISTORY_COLUMNS = (
    "snapshot_id",
    "merchant_id",
    "merchant_name",
    "category",
    "snapshot_ts",
    "history_source",
)
# Stable namespace for content-derived snapshot identities.
SNAPSHOT_NAMESPACE = uuid.UUID("6f1c2a8e-5b7d-4c3e-9a10-2d4e6f8a0b1c")
CHANGE_KINDS = ("category", "name", "name_and_category")


def merchant_history_enabled(cfg: Mapping[str, Any]) -> bool:
    """Return whether the configuration asks for the merchant_history table."""

    section = cfg.get("merchant_history")
    return isinstance(section, Mapping) and section.get("enabled") is True


def validate_merchant_history_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return the merchant_history configuration section."""

    section = cfg.get("merchant_history")
    if not isinstance(section, Mapping):
        raise ValueError("merchant_history configuration must be a mapping")
    interval = section.get("snapshot_interval_days")
    one = section.get("one_change_probability")
    two = section.get("two_change_probability")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        raise ValueError("merchant_history.snapshot_interval_days must be a positive integer")
    for name, value in (("one_change_probability", one), ("two_change_probability", two)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise ValueError(f"merchant_history.{name} must be within [0, 1]")
    if one + two > 1:
        raise ValueError("merchant_history change probabilities must sum to at most 1")
    return {"snapshot_interval_days": interval, "one": float(one), "two": float(two)}


def history_seed(random_seed: int) -> int:
    """Derive a private 32-bit seed so the global generator streams stay untouched."""

    digest = hashlib.sha256(f"merchant_history:{int(random_seed)}".encode("ascii")).hexdigest()
    return int(digest[:8], 16)


def snapshot_times(start: datetime, end: datetime, interval_days: int) -> tuple[datetime, ...]:
    """Return the snapshot schedule ``start, start + interval, ... <= end``."""

    if not start < end:
        raise ValueError("merchant history requires start < end")
    times = []
    current = start
    while current <= end:
        times.append(current)
        current += timedelta(days=interval_days)
    return tuple(times)


def snapshot_id(merchant_id: str, snapshot_ts: datetime) -> str:
    """Content-derived identity: the same merchant and time always yield the same ID."""

    return str(uuid.uuid5(SNAPSHOT_NAMESPACE, f"{merchant_id}|{snapshot_ts.isoformat()}"))


def _previous_state(
    state: tuple[str, str],
    rng: random.Random,
    fake: Faker,
    categories: tuple[str, ...],
) -> tuple[str, str]:
    """Return an earlier state that differs from ``state`` in at least one attribute."""

    name, category = state
    kind = rng.choice(CHANGE_KINDS)
    new_name, new_category = name, category
    if kind in ("name", "name_and_category"):
        while new_name == name:
            new_name = fake.company()
    if kind in ("category", "name_and_category"):
        new_category = rng.choice(tuple(item for item in categories if item != category))
    return new_name, new_category


def generate_merchant_history(
    merchants_df: pd.DataFrame,
    cfg: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Generate full merchant snapshots whose latest state equals ``merchants_df``."""

    settings = validate_merchant_history_config(cfg)
    required = {"merchant_id", "merchant_name", "category"}
    missing = required - set(merchants_df.columns)
    if missing:
        raise ValueError(f"merchants are missing columns for history: {sorted(missing)}")
    times = snapshot_times(start, end, settings["snapshot_interval_days"])
    seed = history_seed(cfg["random_seed"])
    rng = random.Random(seed)
    fake = Faker("vi_VN")
    fake.seed_instance(seed)
    categories = tuple(item.value for item in MerchantCategory)

    rows: list[dict[str, Any]] = []
    for merchant in merchants_df.itertuples(index=False):
        draw = rng.random()
        changes = 2 if draw < settings["two"] else 1 if draw < settings["two"] + settings["one"] else 0
        changes = min(changes, len(times) - 1)
        change_points = sorted(rng.sample(range(1, len(times)), changes))
        # Build segment states backwards from the current state.
        states = [(str(merchant.merchant_name), str(merchant.category))]
        for _ in change_points:
            states.insert(0, _previous_state(states[0], rng, fake, categories))
        boundaries = [0, *change_points, len(times)]
        for segment, state in enumerate(states):
            for index in range(boundaries[segment], boundaries[segment + 1]):
                rows.append({
                    "snapshot_id": snapshot_id(str(merchant.merchant_id), times[index]),
                    "merchant_id": str(merchant.merchant_id),
                    "merchant_name": state[0],
                    "category": state[1],
                    "snapshot_ts": times[index],
                    "history_source": HISTORY_SOURCE,
                })
    frame = pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))
    frame["snapshot_ts"] = pd.to_datetime(frame["snapshot_ts"])
    return frame
