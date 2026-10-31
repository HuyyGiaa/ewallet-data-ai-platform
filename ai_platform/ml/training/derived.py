"""Shared deterministic transformations for training and future serving."""

from __future__ import annotations

import pandas as pd

from ai_platform.ml.training.contract import DERIVED_FEATURE_COLUMNS


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    numerator = pd.to_numeric(numerator, errors="coerce")
    denominator = pd.to_numeric(denominator, errors="coerce")
    result = pd.Series(float("nan"), index=numerator.index, dtype="float64")
    valid = numerator.notna() & denominator.notna() & denominator.ne(0)
    result.loc[valid] = numerator.loc[valid] / denominator.loc[valid]
    return result


def add_derived_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add F4D derived features without imputing unavailable denominators."""
    result = frame.copy()
    amount = result["amount"]
    user_avg = result["user_behavior__user_avg_amount_30d"]
    user_std = result["user_behavior__user_std_amount_30d"]
    account_avg = result["account_behavior__account_avg_amount_30d"]
    account_std = result["account_behavior__account_std_amount_30d"]

    result["amount_ratio_to_user_avg_30d"] = safe_divide(amount, user_avg)
    result["amount_zscore_user_30d"] = safe_divide(
        amount - user_avg, user_std
    )
    result["amount_ratio_to_account_avg_30d"] = safe_divide(
        amount, account_avg
    )
    result["amount_zscore_account_30d"] = safe_divide(
        amount - account_avg, account_std
    )
    result["account_tx_share_1h"] = safe_divide(
        result["account_behavior__account_tx_count_1h"],
        result["user_behavior__user_tx_count_1h"],
    )
    result["merchant_activity_rate_ratio_10m_vs_24h"] = safe_divide(
        result["merchant_behavior__merchant_tx_count_10m"] * 144.0,
        result["merchant_behavior__merchant_tx_count_24h"],
    )
    missing = set(DERIVED_FEATURE_COLUMNS).difference(result.columns)
    if missing:
        raise ValueError(f"Derived features missing: {sorted(missing)}")
    return result
