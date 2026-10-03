"""Deterministic fraud behavior injection for the offline generator.

The transaction contract is intentionally left unchanged. Ground-truth labels
are returned as a separate one-row-per-logical-transaction dataframe.
"""

from __future__ import annotations

import math
import random
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta

import pandas as pd


FRAUD_TYPES = (
    "velocity",
    "amount_anomaly",
    "account_takeover",
    "merchant_burst",
)

CHANNELS = ("app", "web", "atm")


def validate_fraud_config(cfg: dict) -> dict:
    """Validate and return the fraud namespace from generator configuration."""
    fraud_cfg = cfg.get("fraud")
    if not isinstance(fraud_cfg, dict):
        raise ValueError("fraud configuration must be a mapping")

    if not isinstance(fraud_cfg.get("enabled"), bool):
        raise ValueError("fraud.enabled must be true or false")

    seed = fraud_cfg.get("random_seed")
    if not isinstance(seed, int):
        raise ValueError("fraud.random_seed must be an integer")

    prevalence = fraud_cfg.get("prevalence", {})
    target_rate = prevalence.get("target_rate")
    if not isinstance(target_rate, (int, float)) or not 0 <= target_rate < 1:
        raise ValueError("fraud.prevalence.target_rate must be in [0, 1)")

    scenarios = fraud_cfg.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("fraud.scenarios must be a mapping")

    enabled_weights = []
    for name in FRAUD_TYPES:
        scenario = scenarios.get(name)
        if not isinstance(scenario, dict):
            raise ValueError(f"fraud.scenarios.{name} must be a mapping")
        if not isinstance(scenario.get("enabled"), bool):
            raise ValueError(f"fraud.scenarios.{name}.enabled must be true or false")
        weight = scenario.get("weight")
        if not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError(f"fraud.scenarios.{name}.weight must be non-negative")
        if scenario["enabled"]:
            enabled_weights.append(float(weight))

    if fraud_cfg["enabled"] and target_rate > 0:
        if not enabled_weights:
            raise ValueError("at least one fraud scenario must be enabled")
        if not math.isclose(sum(enabled_weights), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("enabled fraud scenario weights must sum to 1.0")

    for name in ("velocity", "merchant_burst"):
        scenario = scenarios[name]
        if not isinstance(scenario.get("burst_size"), int) or scenario["burst_size"] < 2:
            raise ValueError(f"fraud.scenarios.{name}.burst_size must be at least 2")
        if not isinstance(scenario.get("window_minutes"), (int, float)) or scenario["window_minutes"] <= 0:
            raise ValueError(f"fraud.scenarios.{name}.window_minutes must be positive")

    for name in ("amount_anomaly", "account_takeover"):
        scenario = scenarios[name]
        minimum = scenario.get("amount_multiplier_min")
        maximum = scenario.get("amount_multiplier_max")
        if not isinstance(minimum, (int, float)) or minimum <= 0:
            raise ValueError(f"fraud.scenarios.{name}.amount_multiplier_min must be positive")
        if not isinstance(maximum, (int, float)) or maximum < minimum:
            raise ValueError(
                f"fraud.scenarios.{name}.amount_multiplier_max must be >= amount_multiplier_min"
            )

    takeover_probability = scenarios["account_takeover"].get("change_channel_probability")
    if not isinstance(takeover_probability, (int, float)) or not 0 <= takeover_probability <= 1:
        raise ValueError(
            "fraud.scenarios.account_takeover.change_channel_probability must be in [0, 1]"
        )

    return fraud_cfg


def _allocate_counts(total: int, scenarios: dict) -> dict[str, int]:
    enabled = [name for name in FRAUD_TYPES if scenarios[name]["enabled"]]
    raw = {name: total * float(scenarios[name]["weight"]) for name in enabled}
    counts = {name: math.floor(raw[name]) for name in enabled}
    remaining = total - sum(counts.values())
    ranked = sorted(enabled, key=lambda name: (-(raw[name] - counts[name]), FRAUD_TYPES.index(name)))
    for name in ranked[:remaining]:
        counts[name] += 1
    return {name: counts.get(name, 0) for name in FRAUD_TYPES}


def _burst_sizes(total: int, configured_size: int) -> list[int]:
    if total == 0:
        return []
    if total == 1:
        return [1]
    sizes = [configured_size] * (total // configured_size)
    remainder = total % configured_size
    if remainder == 1 and sizes:
        sizes[-1] += 1
    elif remainder:
        sizes.append(remainder)
    return sizes or [total]


def _random_anchor(
    rng: random.Random,
    start: datetime,
    end: datetime,
    window: timedelta,
) -> datetime:
    available_seconds = max(0, int((end - start - window).total_seconds()))
    return start + timedelta(seconds=rng.randint(0, available_seconds))


def _set_event_time(
    transactions: pd.DataFrame,
    index: int,
    timestamp: datetime,
    cutover: datetime,
    rng: random.Random,
) -> None:
    transactions.at[index, "timestamp"] = timestamp
    transactions.at[index, "ingested_at"] = timestamp
    if timestamp < cutover:
        transactions.at[index, "channel"] = None
    elif pd.isna(transactions.at[index, "channel"]):
        transactions.at[index, "channel"] = rng.choice(CHANNELS)


def _cluster_rows(
    transactions: pd.DataFrame,
    indices: list[int],
    window_minutes: float,
    start: datetime,
    end: datetime,
    cutover: datetime,
    rng: random.Random,
) -> float:
    window = timedelta(minutes=float(window_minutes))
    anchor = _random_anchor(rng, start, end, window)
    max_seconds = max(1, int(window.total_seconds()))
    offsets = sorted(rng.randint(0, max_seconds) for _ in indices)
    timestamps = [anchor + timedelta(seconds=offset) for offset in offsets]
    for index, timestamp in zip(indices, timestamps):
        _set_event_time(transactions, index, timestamp, cutover, rng)
    return (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) > 1 else 0.0


def _set_relative_amount(
    transactions: pd.DataFrame,
    index: int,
    account_medians: dict[str, float],
    minimum: float,
    maximum: float,
    rng: random.Random,
) -> None:
    account_id = transactions.at[index, "account_id"]
    baseline = max(float(account_medians.get(account_id, transactions.at[index, "amount"])), 1.0)
    amount = round(baseline * rng.uniform(float(minimum), float(maximum)), 2)
    old_balance = float(transactions.at[index, "old_balance"])
    tx_type = transactions.at[index, "type"]
    status = transactions.at[index, "status"]

    if tx_type == "deposit":
        new_balance = round(old_balance + amount, 2)
    elif status == "failed":
        amount = round(max(amount, old_balance + 1.0), 2)
        new_balance = old_balance
    else:
        amount = round(min(amount, max(old_balance, 1.0)), 2)
        new_balance = round(old_balance - amount, 2)

    transactions.at[index, "amount"] = amount
    transactions.at[index, "new_balance"] = new_balance


def _inject_velocity(
    transactions: pd.DataFrame,
    available: set[int],
    count: int,
    scenario: dict,
    start: datetime,
    end: datetime,
    cutover: datetime,
    rng: random.Random,
) -> tuple[list[int], list[float]]:
    selected: list[int] = []
    durations: list[float] = []
    grouped: dict[str, list[int]] = defaultdict(list)
    for index in sorted(available):
        grouped[str(transactions.at[index, "account_id"])].append(index)
    pools = list(grouped.values())
    rng.shuffle(pools)
    for pool in pools:
        rng.shuffle(pool)
    queue = deque(pool for pool in pools if len(pool) >= 2)

    for size in _burst_sizes(count, scenario["burst_size"]):
        examined = 0
        while queue and len(queue[0]) < size and examined < len(queue):
            queue.rotate(-1)
            examined += 1
        if not queue or len(queue[0]) < size:
            raise ValueError(
                f"not enough rows in one account to build a velocity burst of {size}"
            )
        pool = queue.popleft()
        indices = [pool.pop() for _ in range(size)]
        if len(pool) >= 2:
            queue.append(pool)
        durations.append(
            _cluster_rows(
                transactions,
                indices,
                scenario["window_minutes"],
                start,
                end,
                cutover,
                rng,
            )
        )
        selected.extend(indices)
        available.difference_update(indices)
    return selected, durations


def _inject_merchant_burst(
    transactions: pd.DataFrame,
    available: set[int],
    merchant_ids: list[str],
    count: int,
    scenario: dict,
    start: datetime,
    end: datetime,
    cutover: datetime,
    rng: random.Random,
) -> tuple[list[int], list[float]]:
    payments_by_user: dict[str, list[int]] = defaultdict(list)
    for index in sorted(available):
        if transactions.at[index, "type"] == "payment":
            payments_by_user[str(transactions.at[index, "user_id"])].append(index)
    user_pools = list(payments_by_user.values())
    rng.shuffle(user_pools)
    for pool in user_pools:
        rng.shuffle(pool)
    queue = deque(user_pools)

    selected: list[int] = []
    durations: list[float] = []
    for size in _burst_sizes(count, scenario["burst_size"]):
        if len(queue) < size:
            raise ValueError(
                "not enough distinct users with payment rows to build configured merchant bursts"
            )
        used_pools = [queue.popleft() for _ in range(size)]
        indices = [pool.pop() for pool in used_pools]
        queue.extend(pool for pool in used_pools if pool)

        merchant_id = rng.choice(merchant_ids)
        for index in indices:
            transactions.at[index, "merchant_id"] = merchant_id
            transactions.at[index, "counterparty_account_id"] = None
        durations.append(
            _cluster_rows(
                transactions,
                indices,
                scenario["window_minutes"],
                start,
                end,
                cutover,
                rng,
            )
        )
        selected.extend(indices)
        available.difference_update(indices)
    return selected, durations


def inject_fraud(
    transactions_df: pd.DataFrame,
    devices_df: pd.DataFrame,
    merchants_df: pd.DataFrame,
    cfg: dict,
    start: datetime,
    cutover: datetime,
    end: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Return mutated transactions/devices, separate labels, and metrics."""
    fraud_cfg = validate_fraud_config(cfg)
    transactions = transactions_df.copy(deep=True).reset_index(drop=True)
    devices = devices_df.copy(deep=True).reset_index(drop=True)
    if transactions["transaction_id"].duplicated().any():
        raise ValueError("fraud injection expects unique logical transaction IDs before duplicates")

    labels = pd.DataFrame(
        {
            "transaction_id": transactions["transaction_id"].copy(),
            "label": pd.Series(0, index=transactions.index, dtype="int8"),
            "fraud_type": pd.Series([None] * len(transactions), dtype="object"),
        }
    )
    metrics = {
        "target_count": 0,
        "scenario_counts": {name: 0 for name in FRAUD_TYPES},
        "velocity_burst_durations_seconds": [],
        "merchant_burst_durations_seconds": [],
        "account_takeover_new_devices": 0,
    }
    if not fraud_cfg["enabled"] or fraud_cfg["prevalence"]["target_rate"] == 0:
        return transactions, devices, labels, metrics

    target_count = round(len(transactions) * float(fraud_cfg["prevalence"]["target_rate"]))
    if target_count == 0 and len(transactions):
        target_count = 1
    scenario_counts = _allocate_counts(target_count, fraud_cfg["scenarios"])
    rng = random.Random(fraud_cfg["random_seed"])
    available = set(transactions.index)
    selected_by_type: dict[str, list[int]] = {name: [] for name in FRAUD_TYPES}

    merchant_selected, merchant_durations = _inject_merchant_burst(
        transactions,
        available,
        merchants_df["merchant_id"].tolist(),
        scenario_counts["merchant_burst"],
        fraud_cfg["scenarios"]["merchant_burst"],
        start,
        end,
        cutover,
        rng,
    )
    selected_by_type["merchant_burst"] = merchant_selected

    velocity_selected, velocity_durations = _inject_velocity(
        transactions,
        available,
        scenario_counts["velocity"],
        fraud_cfg["scenarios"]["velocity"],
        start,
        end,
        cutover,
        rng,
    )
    selected_by_type["velocity"] = velocity_selected

    account_medians = transactions.groupby("account_id")["amount"].median().to_dict()
    amount_count = scenario_counts["amount_anomaly"]
    amount_indices = rng.sample(sorted(available), amount_count)
    amount_cfg = fraud_cfg["scenarios"]["amount_anomaly"]
    for index in amount_indices:
        _set_relative_amount(
            transactions,
            index,
            account_medians,
            amount_cfg["amount_multiplier_min"],
            amount_cfg["amount_multiplier_max"],
            rng,
        )
    selected_by_type["amount_anomaly"] = amount_indices
    available.difference_update(amount_indices)

    takeover_count = scenario_counts["account_takeover"]
    takeover_indices = rng.sample(sorted(available), takeover_count)
    takeover_cfg = fraud_cfg["scenarios"]["account_takeover"]
    takeover_by_user: dict[str, list[int]] = defaultdict(list)
    for index in takeover_indices:
        takeover_by_user[str(transactions.at[index, "user_id"])].append(index)

    device_records_by_user = {
        str(user_id): group.to_dict("records")
        for user_id, group in devices.groupby("user_id", sort=False)
    }
    existing_device_ids = set(devices["device_id"])
    new_device_rows = []

    for user_id, indices in takeover_by_user.items():
        templates = device_records_by_user.get(user_id, [])
        if not templates:
            raise ValueError("account_takeover requires an existing device template for the user")
        template = rng.choice(templates)
        new_device_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        while new_device_id in existing_device_ids:
            new_device_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        existing_device_ids.add(new_device_id)

        first_seen_at = min(transactions.at[index, "timestamp"] for index in indices)
        new_device_rows.append(
            {
                "device_id": new_device_id,
                "user_id": user_id,
                "device_type": template["device_type"],
                "os": template["os"],
                "first_seen_at": first_seen_at,
            }
        )

        for index in indices:
            transactions.at[index, "device_id"] = new_device_id
            _set_relative_amount(
                transactions,
                index,
                account_medians,
                takeover_cfg["amount_multiplier_min"],
                takeover_cfg["amount_multiplier_max"],
                rng,
            )
            if (
                transactions.at[index, "timestamp"] >= cutover
                and rng.random() < takeover_cfg["change_channel_probability"]
            ):
                current = transactions.at[index, "channel"]
                transactions.at[index, "channel"] = rng.choice(
                    [channel for channel in CHANNELS if channel != current]
                )

    if new_device_rows:
        devices = pd.concat([devices, pd.DataFrame(new_device_rows)], ignore_index=True)
    selected_by_type["account_takeover"] = takeover_indices

    for fraud_type, indices in selected_by_type.items():
        labels.loc[indices, "label"] = 1
        labels.loc[indices, "fraud_type"] = fraud_type

    metrics.update(
        {
            "target_count": target_count,
            "scenario_counts": {name: len(selected_by_type[name]) for name in FRAUD_TYPES},
            "velocity_burst_durations_seconds": velocity_durations,
            "merchant_burst_durations_seconds": merchant_durations,
            "account_takeover_new_devices": len(new_device_rows),
        }
    )
    return transactions, devices, labels, metrics
