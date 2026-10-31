from __future__ import annotations

import unittest

from ops.gcp.dataproc_delta_smoke import (
    expected_summary,
    fixture_rows,
    validate_run_uri,
)


class DataprocDeltaSmokeFixtureTest(unittest.TestCase):
    def test_fixture_has_stable_unique_rows_and_expected_amount_total(self) -> None:
        rows = fixture_rows()

        self.assertEqual(6, len(rows))
        self.assertEqual(6, len({row["transaction_id"] for row in rows}))
        self.assertAlmostEqual(
            210.75,
            sum(float(row["amount"]) for row in rows),
            places=6,
        )

    def test_fixture_contains_a_timestamp_beyond_millisecond_precision(self) -> None:
        rows = fixture_rows()

        timestamp = rows[1]["event_timestamp"]
        self.assertEqual(123456, timestamp.microsecond)
        self.assertNotEqual(0, timestamp.microsecond % 1000)

    def test_expected_summary_pins_delta_append_and_readback_contract(self) -> None:
        self.assertEqual(
            {
                "initial_rows": 4,
                "final_rows": 6,
                "distinct_transaction_ids": 6,
                "amount_sum": 210.75,
                "initial_delta_version": 0,
                "final_delta_version": 1,
                "microsecond_timestamp": "2026-01-01T00:00:00.123456+00:00",
            },
            expected_summary(),
        )

    def test_run_uri_accepts_only_non_root_gcs_prefixes(self) -> None:
        self.assertEqual(
            "gs://example-temp/dataproc-smoke/delta/r3-smoke-20261022",
            validate_run_uri(
                "gs://example-temp/dataproc-smoke/delta/r3-smoke-20261022/"
            ),
        )

        for invalid in (
            "s3a://example-temp/run",
            "gs://example-temp",
            "gs://example-temp/",
            "gs:///missing-bucket",
            "gs://example-temp/path/../escape",
        ):
            with self.subTest(uri=invalid):
                with self.assertRaises(ValueError):
                    validate_run_uri(invalid)


if __name__ == "__main__":
    unittest.main()
