"""Deterministic physical write layout independent of compute shuffle sizing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class WriteLayout:
    target_tasks: int
    max_records_per_file: int

    def __post_init__(self) -> None:
        if self.target_tasks <= 0 or self.max_records_per_file <= 0:
            raise ValueError("write-layout limits must be positive")

    def evidence(self, partition_columns: Sequence[str]) -> dict[str, Any]:
        return {
            **asdict(self),
            "partition_columns": list(partition_columns),
            "strategy": "fixed_hash_tasks_by_physical_partition_columns",
        }


SILVER_WRITE_LAYOUTS: Mapping[str, WriteLayout] = {
    "transactions": WriteLayout(target_tasks=128, max_records_per_file=2_500_000),
    "balance_snapshots": WriteLayout(target_tasks=48, max_records_per_file=2_500_000),
    "login_events": WriteLayout(target_tasks=48, max_records_per_file=2_500_000),
}

GOLD_WRITE_LAYOUTS: Mapping[str, WriteLayout] = {
    "fact_transactions": WriteLayout(target_tasks=128, max_records_per_file=2_500_000),
    "fact_login_events": WriteLayout(target_tasks=48, max_records_per_file=2_500_000),
    "fact_balance_snapshot": WriteLayout(target_tasks=48, max_records_per_file=2_500_000),
    "obt_transaction_enriched": WriteLayout(
        target_tasks=160, max_records_per_file=2_000_000
    ),
}

FEATURE_WRITE_LAYOUT = WriteLayout(target_tasks=64, max_records_per_file=2_000_000)
DEFAULT_WRITE_LAYOUT = WriteLayout(target_tasks=16, max_records_per_file=2_500_000)


def resolve_write_layout(*, stage: str, table_name: str) -> WriteLayout:
    """Resolve one writer policy without consulting compute shuffle settings."""

    if stage == "dp2-silver":
        return SILVER_WRITE_LAYOUTS.get(table_name, DEFAULT_WRITE_LAYOUT)
    if stage == "dp2-gold":
        return GOLD_WRITE_LAYOUTS.get(table_name, DEFAULT_WRITE_LAYOUT)
    if stage == "dp3-features":
        return FEATURE_WRITE_LAYOUT
    raise ValueError(f"unsupported write-layout stage: {stage}")


def apply_write_layout(
    dataframe: Any,
    *,
    layout: WriteLayout,
    partition_columns: Sequence[str] = (),
) -> Any:
    """Plan a bounded number of output tasks without changing table partitions.

    Hash repartitioning by the existing physical partition columns yields a
    fixed total number of writer tasks. It therefore cannot create
    ``dates × spark.sql.shuffle.partitions`` files by construction.
    """

    if partition_columns:
        return dataframe.repartition(layout.target_tasks, *partition_columns)
    return dataframe.repartition(layout.target_tasks)
