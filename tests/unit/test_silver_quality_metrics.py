from __future__ import annotations

import unittest

from data_platform.processing.spark.silver.quality import (
    validate_transaction_summaries,
)


class SilverQualityMetricsTest(unittest.TestCase):
    def test_consolidated_summary_preserves_row_and_duplicate_gates(self) -> None:
        metrics = validate_transaction_summaries(
            {"rows": 12, "unique_transactions": 10, "channel_nulls": 1},
            {"rows": 10, "unique_transactions": 10, "channel_nulls": 0},
        )
        self.assertEqual(2, metrics.removed_rows)

        with self.assertRaisesRegex(RuntimeError, "duplicates"):
            validate_transaction_summaries(
                {"rows": 10, "unique_transactions": 10, "channel_nulls": 0},
                {"rows": 10, "unique_transactions": 9, "channel_nulls": 0},
            )
        with self.assertRaisesRegex(RuntimeError, "NULL channel"):
            validate_transaction_summaries(
                {"rows": 10, "unique_transactions": 10, "channel_nulls": 0},
                {"rows": 10, "unique_transactions": 10, "channel_nulls": 1},
            )


if __name__ == "__main__":
    unittest.main()
