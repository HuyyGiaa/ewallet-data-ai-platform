"""The source writer pins the canonical timestamp[us] contract independent of pyarrow defaults.

canonical-100gb-v1 shards are timestamp[us] (written by pyarrow 11). Newer pyarrow keeps
pandas' datetime64[ns] unless the unit is pinned, so write_offline_datasets must coerce
to microseconds, keep every value exactly, and refuse to truncate sub-microsecond values.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data_platform.generation.src.offline.offline_generator import write_offline_datasets


class OfflineWriterTimestampContractTest(unittest.TestCase):
    def test_timestamps_are_written_as_microseconds_without_value_change(self) -> None:
        values = [dt.datetime(2026, 1, 1, 1, 2, 3, 456789), dt.datetime(2026, 12, 31, 23, 59, 59, 999999)]
        frame = pd.DataFrame({"event_id": ["a", "b"], "timestamp": values, "amount": [1.5, 2.25]})
        self.assertEqual(str(frame["timestamp"].dtype), "datetime64[ns]")
        with tempfile.TemporaryDirectory() as directory:
            write_offline_datasets({"transactions": frame}, Path(directory))
            table = pq.read_table(Path(directory) / "transactions.parquet")
        self.assertEqual(table.schema.field("timestamp").type, pa.timestamp("us"))
        self.assertEqual(table.column("timestamp").to_pylist(), values)
        self.assertEqual(table.column("amount").to_pylist(), [1.5, 2.25])

    def test_sub_microsecond_timestamps_fail_closed(self) -> None:
        frame = pd.DataFrame({"timestamp": [pd.Timestamp("2026-01-01 00:00:00.000000001")]})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(pa.ArrowInvalid):
                write_offline_datasets({"transactions": frame}, Path(directory))


if __name__ == "__main__":
    unittest.main()
