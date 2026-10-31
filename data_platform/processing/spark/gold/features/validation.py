from __future__ import annotations

from pyspark.sql import DataFrame


def validate_feature_columns(
    df: DataFrame,
    table_name: str,
    required_columns: tuple[str, ...],
) -> None:
    """Fail before a write when a builder violates its declared schema."""
    missing = sorted(set(required_columns) - set(df.columns))
    if missing:
        raise ValueError(f"{table_name} is missing required columns: {missing}")
