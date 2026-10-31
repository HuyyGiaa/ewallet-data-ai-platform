"""Source joining helpers shared by F7 tests and bounded builders."""

from __future__ import annotations

import pandas as pd


def join_transactions_labels(
    transactions: pd.DataFrame,
    labels: pd.DataFrame,
) -> pd.DataFrame:
    """Perform an exact one-to-one label join and reject integrity defects."""
    if transactions["transaction_id"].duplicated().any():
        raise ValueError("Duplicate transaction_id in transactions")
    if labels["transaction_id"].duplicated().any():
        raise ValueError("Duplicate transaction_id in fraud_labels")
    if labels["label"].isna().any():
        raise ValueError("Missing label value in fraud_labels")
    invalid = set(labels["label"].dropna().unique()).difference({0, 1})
    if invalid:
        raise ValueError(f"Invalid labels: {sorted(invalid)}")
    transaction_ids = set(transactions["transaction_id"])
    label_ids = set(labels["transaction_id"])
    if transaction_ids != label_ids:
        missing = len(transaction_ids.difference(label_ids))
        orphan = len(label_ids.difference(transaction_ids))
        raise ValueError(
            f"Label key mismatch: missing={missing} orphan={orphan}"
        )
    result = transactions.merge(
        labels,
        on="transaction_id",
        how="left",
        validate="one_to_one",
        indicator=True,
    )
    return result.drop(columns="_merge")
