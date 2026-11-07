"""Fraud API A/B through real Redis, Feast and two real KServe predictors.

Requires EWALLET_TEST_REDIS_URL, EWALLET_TEST_SERVING_PYTHON and EWALLET_TEST_AB_MODELS,
a directory written by ``ops.experiments.ml_ab_candidate --export-dir``. Set
EWALLET_TEST_AB_EVIDENCE to keep the summaries. Run with the inference interpreter.
Local evidence only: the entities are fixtures, so nothing here measures production.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import pandas as pd

from ai_platform.features.feast import online_operator
from ops.experiments.ml_ab_traffic import http_sender, run_traffic, summarise
from tests.integration import test_feast_online_operator as fixtures
from tests.integration.test_fraud_api_e2e import FRESH_EVENT, _port, _wait

REDIS_URL = os.environ.get("EWALLET_TEST_REDIS_URL")
SERVING_PYTHON = os.environ.get("EWALLET_TEST_SERVING_PYTHON")
MODELS = os.environ.get("EWALLET_TEST_AB_MODELS")
EVIDENCE = os.environ.get("EWALLET_TEST_AB_EVIDENCE")
USERS = [f"ab-user-{index}" for index in range(300)]


def _write_bulk_offline(root: Path) -> None:
    """Write the shared fixture with many users, all inside the freshness window."""
    entities = {**fixtures.ENTITY_VALUES, "user_behavior": USERS}
    for view_name, spec in fixtures.FEATURE_SPECS.items():
        rows = []
        for index, value in enumerate(entities[view_name]):
            row = {spec["entity_key"]: value, "event_timestamp": fixtures.T0 + pd.Timedelta(minutes=115)}
            for offset, feature in enumerate(spec["features"]):
                number = (index % 50) * 10 + offset
                row[feature] = number if feature in spec["integer_features"] else number / 7
            rows.append(row)
        frame = pd.DataFrame(rows)
        frame["event_timestamp"] = frame["event_timestamp"].astype("datetime64[us, UTC]")
        target = root / spec["table"]
        target.mkdir(parents=True)
        frame.to_parquet(target / "part-0.parquet", index=False)


def entity(user: str) -> dict[str, object]:
    """Build one valid payment for a materialized user."""
    return {
        "transaction_id": f"tx-{user}", "event_timestamp": FRESH_EVENT, "user_id": user,
        "account_id": "a1", "device_id": "d1", "merchant_id": "m1",
        "type": "payment", "amount": 125000.0, "currency": "VND", "channel": "app",
    }


@unittest.skipUnless(REDIS_URL and SERVING_PYTHON and MODELS, "requires Redis, serving Python and exported models")
class FraudApiABEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import redis

        redis.Redis.from_url(REDIS_URL).flushdb()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        _write_bulk_offline(cls.root / "offline")
        (cls.root / "registry").mkdir()
        cls.feast_env = {
            "EWALLET_FEAST_REGISTRY": str(cls.root / "registry" / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(cls.root / "offline"),
            "EWALLET_REDIS_CONNECTION_STRING": REDIS_URL.removeprefix("redis://"),
        }
        with patch.dict(os.environ, cls.feast_env), contextlib.redirect_stdout(io.StringIO()):
            assert online_operator.main(["apply"]) == 0
            assert online_operator.main(
                ["materialize", "--start", "2026-05-31T22:00:00+00:00", "--end", "2026-06-01T00:00:00+00:00"]
            ) == 0
        cls.env = {**os.environ, "PYTHONPATH": os.getcwd(), "MLFLOW_DISABLE_AGENT_HINT": "1"}
        cls.servers = {version: cls._start_predictor(version) for version in ("1", "2")}
        cls.results: dict[str, object] = {}

    @classmethod
    def _start_predictor(cls, version: str) -> tuple[subprocess.Popen, int]:
        port = _port()
        process = subprocess.Popen(
            [SERVING_PYTHON, "-m", "ai_platform.serving.fraud_model_server", "--model_name", "fraud-detector",
             "--model-dir", f"{MODELS}/v{version}", "--http_port", str(port), "--enable_grpc", "false"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait(f"http://127.0.0.1:{port}/v2/models/fraud-detector/ready", process)
        return process, port

    @classmethod
    def tearDownClass(cls) -> None:
        for process, _ in cls.servers.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
        cls.temporary.cleanup()
        if EVIDENCE:
            Path(EVIDENCE).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE) / "e2e_summaries.json").write_text(json.dumps(cls.results, indent=2) + "\n")

    def api(self, weights: tuple[int, int]) -> tuple[subprocess.Popen, int, dict[str, int]]:
        arms = [
            {"name": "model-a", "weight": weights[0], "model_version": "1",
             "base_url": f"http://127.0.0.1:{self.servers['1'][1]}", "timeout_seconds": 5.0},
            {"name": "model-b", "weight": weights[1], "model_version": "2",
             "base_url": f"http://127.0.0.1:{self.servers['2'][1]}", "timeout_seconds": 2.0},
        ]
        config = self.root / f"ab-{weights[0]}.json"
        config.write_text(json.dumps({
            "experiment_id": "local-e2e", "salt": "local", "unit": "user_id",
            "model_name": "fraud-detector", "arms": arms,
        }))
        port = _port()
        process = subprocess.Popen(
            [sys.executable, "-m", "ai_platform.inference.server"],
            env={**self.env, **self.feast_env, "PORT": str(port),
                 "EWALLET_FEAST_REPO_PATH": str(online_operator.ONLINE_REPO),
                 "EWALLET_KSERVE_BASE_URL": arms[0]["base_url"], "EWALLET_MODEL_NAME": "fraud-detector",
                 "EWALLET_MODEL_VERSION": "1", "EWALLET_AB_CONFIG_PATH": str(config)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=30)))
        _wait(f"http://127.0.0.1:{port}/health/live", process)
        return process, port, {"model-a": weights[0], "model-b": weights[1]}

    def traffic(self, port: int, configured: dict[str, int], name: str) -> dict:
        send = http_sender(f"http://127.0.0.1:{port}/v1/fraud/predict", timeout=20)
        summary = summarise(run_traffic(send, [entity(user) for user in USERS], 2), configured)
        self.results[name] = summary
        return summary

    def test_1_split_is_sticky_attributed_and_within_tolerance(self) -> None:
        _, port, configured = self.api((90, 10))
        summary = self.traffic(port, configured, "split_90_10")
        self.assertEqual(0, summary["failed_requests"], summary)
        self.assertEqual(0, summary["non_sticky_users"])
        self.assertEqual(0, summary["version_leaks"])
        self.assertTrue(summary["assignment_check"]["within_tolerance"], summary)
        self.assertEqual(["1"], summary["arms"]["model-a"]["model_versions"])
        self.assertEqual(["2"], summary["arms"]["model-b"]["model_versions"])
        with urlopen(f"http://127.0.0.1:{port}/metrics/", timeout=5) as response:
            metrics = response.read().decode()
        self.assertIn('served_arm="model-b"', metrics)
        self.assertIn("fraud_ab_requests_total", metrics)

    def test_2_rollback_sends_nothing_to_the_candidate(self) -> None:
        _, port, configured = self.api((100, 0))
        summary = self.traffic(port, configured, "rollback_100_0")
        self.assertEqual(0, summary["arms"]["model-b"]["requests"])
        self.assertEqual(600, summary["arms"]["model-a"]["requests"])

    def test_3_dead_candidate_falls_back_without_failing_requests(self) -> None:
        _, port, configured = self.api((50, 50))
        process, _ = self.servers["2"]
        process.terminate()
        process.wait(timeout=30)
        summary = self.traffic(port, configured, "candidate_down_50_50")
        self.assertEqual(0, summary["failed_requests"], summary)
        self.assertGreater(summary["fallbacks"], 0)
        self.assertEqual(0, summary["arms"]["model-b"]["requests"])


if __name__ == "__main__":
    unittest.main()
