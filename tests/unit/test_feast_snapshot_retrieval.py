"""Genuine Feast historical retrieval over a small local snapshot."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.validation import snapshot_retrieval

AS_OF = datetime(2026, 5, 20, 12, 0, 0, 500000, tzinfo=timezone.utc)


def write_snapshot(root: Path, *, tie: bool = False) -> str:
    """Two rows per entity before ``as_of`` plus one after it that Feast must ignore."""

    tables = {}
    for view, spec in FEATURE_SPECS.items():
        rows = []
        for index in range(4):
            entity = f"{view}-e{index}"
            stamps = [AS_OF - timedelta(hours=3), AS_OF - timedelta(minutes=7, microseconds=11)]
            stamps.append(AS_OF + timedelta(seconds=1))
            if tie and index == 0:
                stamps.append(stamps[1])
            for offset, stamp in enumerate(stamps):
                row = {spec["entity_key"]: entity, "event_timestamp": pd.Timestamp(stamp)}
                for position, feature in enumerate(spec["features"]):
                    value = index * 1000 + offset * 10 + position
                    row[feature] = int(value) if feature in spec["integer_features"] else value + 0.25
                rows.append(row)
        frame = pd.DataFrame(rows)
        frame["event_timestamp"] = frame["event_timestamp"].astype("datetime64[us, UTC]")
        target = root / spec["table"]
        target.mkdir(parents=True)
        frame.to_parquet(target / "part-0.parquet", index=False)
        tables[view] = {"target": f"{root}/{spec['table']}", "rows": len(frame)}
    control = root / "_control"
    control.mkdir()
    raw = json.dumps({"dataset_version": "test", "tables": tables}).encode()
    (control / "export_manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class SnapshotRetrievalTest(unittest.TestCase):
    def test_feast_point_in_time_rows_match_the_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            digest = write_snapshot(root)
            report = snapshot_retrieval.verify(str(root), digest, AS_OF, timedelta(hours=6), 3)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["comparisons"], 3 * report["historical_features"])
        self.assertEqual(report["mismatch_count"], 0)
        for view in report["views"]:
            self.assertEqual(
                set(view["selected_event_timestamps"]),
                {pd.Timestamp(AS_OF - timedelta(minutes=7, microseconds=11)).isoformat()},
            )

    def test_wrong_manifest_digest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_snapshot(root)
            with self.assertRaisesRegex(ValueError, "pinned"):
                snapshot_retrieval.verify(str(root), "0" * 64, AS_OF, timedelta(hours=6), 3)

    def test_tied_latest_rows_fail_instead_of_being_compared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            digest = write_snapshot(root, tie=True)
            report = snapshot_retrieval.verify(str(root), digest, AS_OF, timedelta(hours=6), 4)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["ambiguous_entities"], len(FEATURE_SPECS))


if __name__ == "__main__":
    unittest.main()
