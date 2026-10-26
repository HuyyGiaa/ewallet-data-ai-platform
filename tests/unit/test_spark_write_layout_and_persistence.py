from __future__ import annotations

import unittest
from unittest.mock import Mock

from data_platform.processing.spark.common.persistence import persisted
from data_platform.processing.spark.common.write_layout import (
    GOLD_WRITE_LAYOUTS,
    SILVER_WRITE_LAYOUTS,
    apply_write_layout,
)


class SparkWriteLayoutAndPersistenceTest(unittest.TestCase):
    def test_write_layout_is_independent_of_shuffle_partitions(self) -> None:
        layout = GOLD_WRITE_LAYOUTS["obt_transaction_enriched"]
        self.assertEqual(160, layout.target_tasks)
        self.assertNotEqual(400, layout.target_tasks)
        self.assertEqual(
            ["event_date"], layout.evidence(("event_date",))["partition_columns"]
        )

    def test_obt_plan_cannot_create_dates_times_shuffle_tasks(self) -> None:
        dataframe = Mock()
        planned = Mock()
        dataframe.repartition.return_value = planned
        result = apply_write_layout(
            dataframe,
            layout=GOLD_WRITE_LAYOUTS["obt_transaction_enriched"],
            partition_columns=("event_date",),
        )
        dataframe.repartition.assert_called_once_with(160, "event_date")
        self.assertIs(result, planned)

    def test_silver_canonical_partition_columns_are_preserved(self) -> None:
        self.assertIn("transactions", SILVER_WRITE_LAYOUTS)
        self.assertEqual(
            ["event_date"],
            SILVER_WRITE_LAYOUTS["transactions"].evidence(("event_date",))[
                "partition_columns"
            ],
        )

    def test_unpartitioned_layout_uses_fixed_tasks_not_input_partition_count(self) -> None:
        dataframe = Mock()
        planned = Mock()
        dataframe.repartition.return_value = planned
        result = apply_write_layout(
            dataframe,
            layout=SILVER_WRITE_LAYOUTS["transactions"],
        )
        dataframe.repartition.assert_called_once_with(128)
        self.assertIs(result, planned)

    def test_persistence_owner_unpersists_on_success_and_failure(self) -> None:
        for should_fail in (False, True):
            source = Mock()
            cached = Mock()
            source.persist.return_value = cached
            try:
                with persisted(source, "DISK_ONLY") as reusable:
                    self.assertIs(reusable, cached)
                    if should_fail:
                        raise RuntimeError("synthetic write failure")
            except RuntimeError:
                pass
            cached.unpersist.assert_called_once_with(blocking=False)


if __name__ == "__main__":
    unittest.main()
