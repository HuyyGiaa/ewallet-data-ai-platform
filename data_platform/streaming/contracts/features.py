"""Ownership classification for the immutable 31-feature DP3 contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ai_platform.features.feast.schema import FEATURE_SPECS


class Ownership(str, Enum):
    REALTIME = "REALTIME"
    MATERIALIZED_BATCH = "MATERIALIZED_BATCH"
    HYBRID = "HYBRID"


@dataclass(frozen=True)
class FeatureOwnership:
    name: str
    view_name: str
    entity: str
    entity_key: str
    definition: str
    required_state: str
    realtime_maintainability: str
    bootstrap_requirement: str
    ownership: Ownership


_DETAILS = {
    "user_tx_count_5m": ("count [T-5m,T)", "5m user events"),
    "user_tx_count_1h": ("count [T-1h,T)", "1h user events"),
    "user_tx_count_24h": ("count [T-24h,T)", "24h user events"),
    "user_amount_sum_1h": ("amount sum [T-1h,T)", "1h user amounts"),
    "user_amount_observation_count_30d": ("non-null amount count [T-30d,T)", "30d user amounts"),
    "user_avg_amount_30d": ("amount mean [T-30d,T)", "30d user amounts"),
    "user_std_amount_30d": ("sample amount stddev [T-30d,T)", "30d user amounts"),
    "user_failed_rate_24h": ("failed/count [T-24h,T)", "24h user status"),
    "user_distinct_merchants_24h": ("distinct non-null merchants [T-24h,T)", "24h user merchant multiset"),
    "account_tx_count_5m": ("count [T-5m,T)", "5m account events"),
    "account_tx_count_1h": ("count [T-1h,T)", "1h account events"),
    "account_tx_count_24h": ("count [T-24h,T)", "24h account events"),
    "account_amount_sum_1h": ("amount sum [T-1h,T)", "1h account amounts"),
    "account_amount_sum_24h": ("amount sum [T-24h,T)", "24h account amounts"),
    "account_amount_observation_count_30d": ("non-null amount count [T-30d,T)", "30d account amounts"),
    "account_avg_amount_30d": ("amount mean [T-30d,T)", "30d account amounts"),
    "account_std_amount_30d": ("sample amount stddev [T-30d,T)", "30d account amounts"),
    "account_failed_rate_24h": ("failed/count [T-24h,T)", "24h account status"),
    "account_seconds_since_last_tx": ("T minus latest event strictly before T", "latest account event time"),
    "device_age_seconds": ("max(0,T-first_seen_at)", "device first_seen dimension"),
    "device_tx_count_1h": ("count [T-1h,T)", "1h device events"),
    "device_tx_count_24h": ("count [T-24h,T)", "24h device events"),
    "device_amount_sum_24h": ("amount sum [T-24h,T)", "24h device amounts"),
    "device_failed_rate_24h": ("failed/count [T-24h,T)", "24h device status"),
    "merchant_tx_count_10m": ("payment count [T-10m,T)", "10m merchant payment events"),
    "merchant_tx_count_1h": ("payment count [T-1h,T)", "1h merchant payment events"),
    "merchant_tx_count_24h": ("payment count [T-24h,T)", "24h merchant payment events"),
    "merchant_unique_users_10m": ("distinct users [T-10m,T)", "10m merchant user multiset"),
    "merchant_unique_users_1h": ("distinct users [T-1h,T)", "1h merchant user multiset"),
    "merchant_amount_sum_1h": ("payment amount sum [T-1h,T)", "1h merchant amounts"),
    "merchant_avg_amount_24h": ("payment amount mean [T-24h,T)", "24h merchant amounts"),
}


FEATURE_OWNERSHIP = tuple(
    FeatureOwnership(
        name=feature,
        view_name=view_name,
        entity=str(spec["entity_name"]),
        entity_key=str(spec["entity_key"]),
        definition=_DETAILS[feature][0],
        required_state=_DETAILS[feature][1],
        realtime_maintainability="exact after bounded history/dimension bootstrap",
        bootstrap_requirement=(
            "load canonical Silver devices.first_seen_at"
            if feature == "device_age_seconds"
            else (
                "replay immutable Silver transactions [T0-30d,T0) plus latest pre-window account anchor"
                if feature == "account_seconds_since_last_tx"
                else "replay immutable Silver transactions [T0-30d,T0)"
            )
        ),
        ownership=Ownership.HYBRID,
    )
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["features"]
)

if len(FEATURE_OWNERSHIP) != 31 or {item.name for item in FEATURE_OWNERSHIP} != set(_DETAILS):
    raise RuntimeError("streaming ownership must exactly match the 31-feature DP3 contract")
