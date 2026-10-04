"""Canonical F5-to-Feast schema mapping.

This module deliberately has no Feast or Spark dependency so both runtime
environments can use the same source-of-truth mapping.
"""

from __future__ import annotations


FEATURE_SPECS = {
    "user_behavior": {
        "table": "feat_user_behavior",
        "entity_name": "user",
        "entity_key": "user_id",
        "features": (
            "user_tx_count_5m",
            "user_tx_count_1h",
            "user_tx_count_24h",
            "user_amount_sum_1h",
            "user_amount_observation_count_30d",
            "user_avg_amount_30d",
            "user_std_amount_30d",
            "user_failed_rate_24h",
            "user_distinct_merchants_24h",
        ),
        "integer_features": (
            "user_tx_count_5m",
            "user_tx_count_1h",
            "user_tx_count_24h",
            "user_amount_observation_count_30d",
            "user_distinct_merchants_24h",
        ),
    },
    "account_behavior": {
        "table": "feat_account_behavior",
        "entity_name": "account",
        "entity_key": "account_id",
        "features": (
            "account_tx_count_5m",
            "account_tx_count_1h",
            "account_tx_count_24h",
            "account_amount_sum_1h",
            "account_amount_sum_24h",
            "account_amount_observation_count_30d",
            "account_avg_amount_30d",
            "account_std_amount_30d",
            "account_failed_rate_24h",
            "account_seconds_since_last_tx",
        ),
        "integer_features": (
            "account_tx_count_5m",
            "account_tx_count_1h",
            "account_tx_count_24h",
            "account_amount_observation_count_30d",
        ),
    },
    "device_behavior": {
        "table": "feat_device_behavior",
        "entity_name": "device",
        "entity_key": "device_id",
        "features": (
            "device_age_seconds",
            "device_tx_count_1h",
            "device_tx_count_24h",
            "device_amount_sum_24h",
            "device_failed_rate_24h",
        ),
        "integer_features": (
            "device_tx_count_1h",
            "device_tx_count_24h",
        ),
    },
    "merchant_behavior": {
        "table": "feat_merchant_behavior",
        "entity_name": "merchant",
        "entity_key": "merchant_id",
        "features": (
            "merchant_tx_count_10m",
            "merchant_tx_count_1h",
            "merchant_tx_count_24h",
            "merchant_unique_users_10m",
            "merchant_unique_users_1h",
            "merchant_amount_sum_1h",
            "merchant_avg_amount_24h",
        ),
        "integer_features": (
            "merchant_tx_count_10m",
            "merchant_tx_count_1h",
            "merchant_tx_count_24h",
            "merchant_unique_users_10m",
            "merchant_unique_users_1h",
        ),
    },
}

ALL_FEATURE_REFS = tuple(
    f"{view_name}:{feature}"
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["features"]
)
