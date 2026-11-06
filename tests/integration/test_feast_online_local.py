"""Real Feast and real Redis checks for the C1/C2 online write contract.

Runs the actual operator against a throwaway Redis, because a mock cannot prove
Feast's timestamp guard. Set ``EWALLET_TEST_REDIS_CONNECTION`` to a loopback Redis,
for example ``127.0.0.1:16399,db=3``. The suite refuses any other host, and it
flushes the selected database. Results are local validation, not cloud evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONNECTION = os.environ.get("EWALLET_TEST_REDIS_CONNECTION", "")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
T0 = datetime(2026, 5, 31, 21, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)


def _is_loopback(connection: str) -> bool:
    return connection.split(",")[0].split(":")[0] in {"127.0.0.1", "localhost"}


def _flush(connection: str) -> None:
    import redis

    host, port = connection.split(",")[0].split(":")
    options = dict(item.split("=", 1) for item in connection.split(",")[1:])
    redis.Redis(host=host, port=int(port), db=int(options.get("db", 0))).flushdb()


@unittest.skipUnless(CONNECTION, "EWALLET_TEST_REDIS_CONNECTION is not set")
class OnlineOperatorLocalTest(unittest.TestCase):
    """One registry and one Redis database per test."""

    def setUp(self):
        self.assertTrue(_is_loopback(CONNECTION), "tests only run against a loopback Redis")
        from ai_platform.features.feast.schema import FEATURE_SPECS

        self.specs = FEATURE_SPECS
        self.root = Path(tempfile.mkdtemp(prefix="feast-online-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.registry = self.root / "registry" / "registry.db"
        self.registry.parent.mkdir()
        self.data_root = self.root / "offline"
        self.environment = {
            **os.environ,
            "EWALLET_FEAST_REGISTRY": str(self.registry),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(self.data_root),
            "EWALLET_REDIS_CONNECTION_STRING": CONNECTION,
            "EWALLET_GIT_SHA": "local",
            "PYTHONPATH": str(REPOSITORY_ROOT),
        }
        os.environ.update(
            {key: self.environment[key] for key in (
                "EWALLET_FEAST_REGISTRY", "EWALLET_FEAST_OFFLINE_DATA_ROOT",
                "EWALLET_REDIS_CONNECTION_STRING")}
        )
        _flush(CONNECTION)
        self.write_snapshot()

    def write_snapshot(self):
        """Two entities per view, one row in each hour, value = hour number."""
        import pandas as pd

        for view, spec in self.specs.items():
            rows = []
            for hour, stamp in enumerate((T0 + timedelta(minutes=30), T1 + timedelta(minutes=30),
                                          T2 + timedelta(minutes=30)), start=1):
                for entity in ("e1", "e2"):
                    row = {spec["entity_key"]: entity, "event_timestamp": stamp}
                    for feature in spec["features"]:
                        integer = feature in spec["integer_features"]
                        row[feature] = hour if integer else float(hour)
                    rows.append(row)
            frame = pd.DataFrame(rows)
            for feature in spec["features"]:
                if feature in spec["integer_features"]:
                    frame[feature] = frame[feature].astype("int64")
            frame["event_timestamp"] = pd.to_datetime(frame["event_timestamp"], utc=True)
            directory = self.data_root / spec["table"]
            directory.mkdir(parents=True)
            frame.to_parquet(directory / "part-0.parquet", index=False)

    def operator(self, *arguments: str, check: bool = True) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "ai_platform.features.feast.online_operator", *arguments],
            capture_output=True, text=True, env=self.environment, cwd=REPOSITORY_ROOT, check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"operator {arguments} failed:\n{result.stderr[-2000:]}")
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        return json.loads(lines[-1]) if lines else {"returncode": result.returncode, "stderr": result.stderr}

    def store(self):
        from ai_platform.features.feast.online_operator import get_online_store

        return get_online_store()

    def online(self, view="user_behavior", entity="e1"):
        spec = self.specs[view]
        refs = [f"{view}:{spec['features'][0]}"]
        result = self.store().get_online_features(
            features=refs, entity_rows=[{spec["entity_key"]: entity}], full_feature_names=True
        ).to_dict(include_event_timestamps=True)
        name = f"{view}__{spec['features'][0]}"
        return result[name][0], result[f"{name}__ts"][0]

    def push(self, stamp, value, view="user_behavior", entity="e1"):
        """Write through the real streaming writer, as the Feast consumer does."""
        from data_platform.streaming.feast.online import FeastPushWriter
        from data_platform.streaming.sinks.online import OnlineFeatureUpdate

        spec = self.specs[view]
        values = {
            feature: int(value) if feature in spec["integer_features"] else float(value)
            for feature in spec["features"]
        }
        update = OnlineFeatureUpdate(view, spec["entity_name"], spec["entity_key"], entity, stamp, values, "evt")
        FeastPushWriter(self.store()).write(update)

    def load_base_window(self):
        self.operator("apply")
        self.operator("materialize", "--start", T0.isoformat(), "--end", T1.isoformat())

    def test_incremental_continues_where_the_registry_stopped(self):
        self.load_base_window()
        self.assertEqual(1, self.online()[0])
        evidence = self.operator("materialize-incremental", "--end", T2.isoformat())
        self.assertEqual({T1.isoformat()}, {view["start"] for view in evidence["views"]})
        self.assertEqual(2, self.online()[0])

    def test_a_never_materialized_view_is_refused(self):
        self.operator("apply")
        result = self.operator("materialize-incremental", "--end", T2.isoformat(), check=False)
        self.assertNotEqual(0, result.get("returncode"))
        self.assertIn("never materialized", result["stderr"])

    def test_repeating_a_window_is_a_no_op(self):
        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        again = self.operator("materialize-incremental", "--end", T2.isoformat())
        self.assertEqual({"already current"}, {view.get("skipped") for view in again["views"]})
        self.assertEqual(2, self.online()[0])

    def test_state_reports_registry_and_snapshot_bounds(self):
        self.load_base_window()
        evidence = self.operator("state")
        views = {view["view"]: view for view in evidence["views"]}
        self.assertEqual(set(self.specs), set(views))
        self.assertEqual(T1.isoformat(), views["user_behavior"]["materialized_end"])
        self.assertEqual((T2 + timedelta(minutes=30)).isoformat(),
                         views["user_behavior"]["source_max_event_timestamp"])
        self.assertEqual(1, views["user_behavior"]["source_files"])

    def test_state_feeds_the_window_planner(self):
        from ai_platform.features.feast.materialization_plan import ViewState

        self.load_base_window()
        for view in self.operator("state")["views"]:
            self.assertEqual(T1, ViewState.from_evidence(view).materialized_end)

    def test_parity_reports_real_event_timestamps(self):
        self.load_base_window()
        evidence = self.operator(
            "parity", "--cutover", T1.isoformat(), "--sample-window-start", T0.isoformat(),
            "--sample-size", "1",
        )
        stamp = evidence["views"][0]["online_event_timestamps"][0]
        self.assertEqual((T0 + timedelta(minutes=30)).isoformat(), stamp)

    def test_parity_passes_after_materialization(self):
        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        evidence = self.operator(
            "parity", "--cutover", T2.isoformat(), "--sample-window-start", T1.isoformat(),
            "--sample-size", "2",
        )
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual(62, evidence["comparisons"])

    def test_readback_verifier_accepts_real_parity_evidence(self):
        from ai_platform.features.feast.materialization_plan import verify_readback

        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        evidence = self.operator(
            "parity", "--cutover", T2.isoformat(), "--sample-window-start", T1.isoformat(),
            "--sample-size", "2",
        )
        self.assertEqual(8, verify_readback(evidence, start=T1, end=T2)["entities_checked"])

    def test_an_older_window_cannot_replace_a_newer_streaming_value(self):
        self.load_base_window()
        newer = T3 + timedelta(minutes=5)
        self.push(newer, 99)
        self.operator("materialize-incremental", "--end", T2.isoformat())
        value, stamp = self.online()
        self.assertEqual(99, value)
        self.assertEqual(int(newer.timestamp()), stamp)

    def test_a_stale_job_replaying_an_old_window_changes_nothing(self):
        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        self.operator("materialize", "--start", T0.isoformat(), "--end", T1.isoformat())
        self.assertEqual(2, self.online()[0])

    def test_a_late_stream_update_cannot_replace_a_newer_batch_value(self):
        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        self.push(T0 + timedelta(minutes=1), 77)
        self.assertEqual(2, self.online()[0])

    def test_a_replayed_stream_update_is_ignored(self):
        stamp = T3 + timedelta(minutes=5)
        self.operator("apply")
        self.push(stamp, 5)
        self.push(stamp, 6)
        self.assertEqual(5, self.online()[0])

    def test_a_later_sub_second_stream_update_is_applied(self):
        stamp = T3 + timedelta(minutes=5)
        self.operator("apply")
        self.push(stamp, 5)
        self.push(stamp + timedelta(milliseconds=1), 6)
        self.assertEqual(6, self.online()[0])

    def test_views_are_guarded_independently_for_one_entity(self):
        self.load_base_window()
        self.push(T3, 42, view="device_behavior")
        self.operator("materialize-incremental", "--end", T2.isoformat())
        self.assertEqual(2, self.online("user_behavior")[0])
        self.assertEqual(42, self.online("device_behavior")[0])

    def test_parity_reports_streaming_owned_entities_instead_of_failing(self):
        self.load_base_window()
        self.operator("materialize-incremental", "--end", T2.isoformat())
        self.push(T3, 99)
        evidence = self.operator(
            "parity", "--cutover", T2.isoformat(), "--sample-window-start", T1.isoformat(),
            "--sample-size", "2",
        )
        user = next(view for view in evidence["views"] if view["view"] == "user_behavior")
        self.assertEqual(["e1"], user["superseded_by_streaming"])
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual(62 - 9, evidence["comparisons"])

    def test_a_null_feature_survives_the_push_path(self):
        from data_platform.streaming.feast.online import FeastPushWriter
        from data_platform.streaming.sinks.online import OnlineFeatureUpdate

        self.operator("apply")
        spec = self.specs["user_behavior"]
        values = {
            feature: 0 if feature in spec["integer_features"] else None
            for feature in spec["features"]
        }
        FeastPushWriter(self.store()).write(
            OnlineFeatureUpdate("user_behavior", "user", "user_id", "e9", T3, values, "evt")
        )
        result = self.store().get_online_features(
            features=["user_behavior:user_avg_amount_30d", "user_behavior:user_tx_count_1h"],
            entity_rows=[{"user_id": "e9"}],
        ).to_dict()
        self.assertIsNone(result["user_avg_amount_30d"][0])
        self.assertEqual(0, result["user_tx_count_1h"][0])

    def test_a_source_outside_the_snapshot_stops_incremental(self):
        self.load_base_window()
        self.environment["EWALLET_FEAST_OFFLINE_DATA_ROOT"] = str(self.root / "other")
        result = self.operator("materialize-incremental", "--end", T2.isoformat(), check=False)
        self.assertNotEqual(0, result.get("returncode"))
        self.assertIn("outside snapshot", result["stderr"])


if __name__ == "__main__":
    unittest.main()
