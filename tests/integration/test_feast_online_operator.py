"""Feast Online operator against a real Redis (set EWALLET_TEST_REDIS_URL)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from ai_platform.features.feast import online_operator
from ai_platform.features.feast.schema import FEATURE_SPECS


REDIS_URL = os.environ.get("EWALLET_TEST_REDIS_URL")
ENTITY_VALUES = {
    "user_behavior": ("u1", "u2", "u3"),
    "account_behavior": ("a1", "a2", "a3"),
    "device_behavior": ("d1", "d2", "d3"),
    "merchant_behavior": ("m1", "m2", "m3"),
}
T0 = pd.Timestamp("2026-05-31 22:00:00", tz="UTC")


def _write_offline(root: Path) -> None:
    for view_name, spec in FEATURE_SPECS.items():
        rows = []
        for entity_index, entity in enumerate(ENTITY_VALUES[view_name]):
            # three updates per entity; the last lands in the incremental window
            for step, minutes in enumerate((5, 50, 115)):
                row = {
                    spec["entity_key"]: entity,
                    "event_timestamp": T0 + pd.Timedelta(minutes=minutes + entity_index),
                }
                for feature_index, feature in enumerate(spec["features"]):
                    value = entity_index * 1000 + step * 100 + feature_index
                    row[feature] = (
                        value if feature in spec["integer_features"] else value / 7
                    )
                rows.append(row)
        frame = pd.DataFrame(rows)
        frame["event_timestamp"] = frame["event_timestamp"].astype("datetime64[us, UTC]")
        target = root / spec["table"]
        target.mkdir(parents=True)
        frame.to_parquet(target / "part-0.parquet", index=False)


@unittest.skipUnless(REDIS_URL, "requires a disposable Redis (EWALLET_TEST_REDIS_URL)")
class FeastOnlineOperatorTest(unittest.TestCase):
    def run_command(self, *argv: str) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = online_operator.main(list(argv))
        evidence = json.loads(output.getvalue().strip().splitlines()[-1])
        evidence["exit_code"] = code
        return evidence

    def test_apply_materialize_incremental_and_parity(self) -> None:
        import redis

        redis.Redis.from_url(REDIS_URL).flushdb()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_offline(root / "offline")
            host_port = REDIS_URL.removeprefix("redis://")
            environment = {
                "EWALLET_FEAST_REGISTRY": str(root / "registry" / "registry.db"),
                "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(root / "offline"),
                "EWALLET_REDIS_CONNECTION_STRING": host_port,
            }
            with patch.dict(os.environ, environment):
                (root / "registry").mkdir()
                applied = self.run_command("apply")
                self.assertEqual(sorted(FEATURE_SPECS), applied["feature_views"])
                with self.assertRaises(FileExistsError):
                    self.run_command("apply")

                window = self.run_command(
                    "materialize",
                    "--start", "2026-05-31T22:00:00+00:00",
                    "--end", "2026-05-31T23:00:00+00:00",
                )
                self.assertEqual(4, len(window["views"]))
                incremental = self.run_command(
                    "materialize-incremental", "--end", "2026-06-01T00:00:00+00:00"
                )
                for view in incremental["views"]:
                    self.assertEqual("2026-05-31T23:00:00+00:00", view["start"])

                parity = self.run_command(
                    "parity",
                    "--cutover", "2026-06-01T00:00:00+00:00",
                    "--sample-window-start", "2026-05-31T23:50:00+00:00",
                    "--sample-size", "3",
                )
                expected = 3 * sum(len(s["features"]) for s in FEATURE_SPECS.values())
                self.assertEqual("PASS", parity["status"])
                self.assertEqual(0, parity["exit_code"])
                self.assertEqual(expected, parity["comparisons"])
                self.assertEqual(31, parity["historical_features"])

                # Repeating the same window is idempotent for the stored values.
                self.run_command(
                    "materialize",
                    "--start", "2026-05-31T23:00:00+00:00",
                    "--end", "2026-06-01T00:00:00+00:00",
                )
                again = self.run_command(
                    "parity",
                    "--cutover", "2026-06-01T00:00:00+00:00",
                    "--sample-window-start", "2026-05-31T23:50:00+00:00",
                    "--sample-size", "3",
                )
                self.assertEqual("PASS", again["status"])


if __name__ == "__main__":
    unittest.main()
