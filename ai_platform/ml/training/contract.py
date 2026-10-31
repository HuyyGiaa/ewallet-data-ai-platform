"""Executable column contract for the F7 fraud training dataset."""

from __future__ import annotations

from ai_platform.features.feast.schema import FEATURE_SPECS


IDENTIFIER_COLUMNS = (
    "transaction_id",
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
)

REQUEST_TIME_FEATURE_COLUMNS = (
    "amount",
    "type",
    "channel",
    "merchant_present",
)

HISTORICAL_FEATURE_COLUMNS = tuple(
    f"{view_name}__{feature}"
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["features"]
)

DERIVED_FEATURE_COLUMNS = (
    "amount_ratio_to_user_avg_30d",
    "amount_zscore_user_30d",
    "amount_ratio_to_account_avg_30d",
    "amount_zscore_account_30d",
    "account_tx_share_1h",
    "merchant_activity_rate_ratio_10m_vs_24h",
)

MODEL_FEATURE_COLUMNS = (
    *REQUEST_TIME_FEATURE_COLUMNS,
    *HISTORICAL_FEATURE_COLUMNS,
    *DERIVED_FEATURE_COLUMNS,
)

NON_FEATURE_COLUMNS = (
    "transaction_id",
    "event_timestamp",
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "label",
    "fraud_type",
    "split",
)

OUTPUT_COLUMNS = (*NON_FEATURE_COLUMNS, *MODEL_FEATURE_COLUMNS)

FORBIDDEN_MODEL_COLUMNS = frozenset(
    {
        "transaction_id",
        "user_id",
        "account_id",
        "device_id",
        "merchant_id",
        "label",
        "fraud_type",
        "status",
        "new_balance",
        "ingested_at",
    }
)

FORBIDDEN_OUTPUT_COLUMNS = frozenset({"status", "new_balance", "ingested_at"})


def validate_contract() -> None:
    overlap = FORBIDDEN_MODEL_COLUMNS.intersection(MODEL_FEATURE_COLUMNS)
    if overlap:
        raise ValueError(f"Forbidden model features: {sorted(overlap)}")
    if len(MODEL_FEATURE_COLUMNS) != len(set(MODEL_FEATURE_COLUMNS)):
        raise ValueError("MODEL_FEATURE_COLUMNS contains duplicates")


validate_contract()
