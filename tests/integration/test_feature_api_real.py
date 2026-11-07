"""Feature API on real Feast Online and a real Redis, through a real server process.

Requires EWALLET_TEST_REDIS_URL pointing at a disposable Redis. Run with the inference
interpreter. The stopped-Redis case runs last because it ends the backend connection.
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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_platform.features.feast import online_operator
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
from tests.integration.test_feast_online_operator import _write_offline
from tests.integration.test_fraud_api_e2e import FRESH_EVENT, _port, _wait

REDIS_URL = os.environ.get("EWALLET_TEST_REDIS_URL")
NAMES = list(HISTORICAL_FEATURE_COLUMNS[:3])


def post(url: str, body: dict) -> tuple[int, dict]:
    request = Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@unittest.skipUnless(REDIS_URL, "requires a disposable Redis (EWALLET_TEST_REDIS_URL)")
class FeatureApiRealBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import redis

        redis.Redis.from_url(REDIS_URL).flushdb()
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        _write_offline(root / "offline")
        (root / "registry").mkdir()
        cls.env = {
            "EWALLET_FEAST_REGISTRY": str(root / "registry" / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(root / "offline"),
            "EWALLET_REDIS_CONNECTION_STRING": REDIS_URL.removeprefix("redis://"),
        }
        previous = {key: os.environ.get(key) for key in cls.env}
        os.environ.update(cls.env)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                assert online_operator.main(["apply"]) == 0
                assert online_operator.main(
                    ["materialize", "--start", "2026-05-31T22:00:00+00:00", "--end", "2026-06-01T00:00:00+00:00"]
                ) == 0
        finally:
            for key, value in previous.items():
                os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        cls.port = _port()
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "ai_platform.inference.feature_server"],
            env={**os.environ, **cls.env, "PYTHONPATH": os.getcwd(), "PORT": str(cls.port),
                 "EWALLET_FEAST_REPO_PATH": str(online_operator.ONLINE_REPO), "EWALLET_FEAST_TIMEOUT_SECONDS": "2"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait(f"http://127.0.0.1:{cls.port}/health/live", cls.process)
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        cls.process.wait(timeout=30)
        cls.temporary.cleanup()

    def lookup(self, **changes) -> tuple[int, dict]:
        body = {"user_id": "u1", "account_id": "a1", "device_id": "d1", "merchant_id": "m1",
                "event_timestamp": FRESH_EVENT, "feature_names": NAMES, **changes}
        return post(f"{self.url}/v1/features/lookup", body)

    def test_ready_checks_the_real_store(self) -> None:
        with urlopen(f"{self.url}/health/ready", timeout=20) as response:
            self.assertEqual(200, response.status)

    def test_lookup_returns_real_values_for_the_requested_names_only(self) -> None:
        status, body = self.lookup()
        self.assertEqual(200, status, body)
        self.assertEqual(set(NAMES), set(body["features"]))

    def test_unknown_entity_is_missing(self) -> None:
        status, body = self.lookup(user_id="nobody")
        self.assertEqual((424, "missing_feature"), (status, body["error"]["code"]))

    def test_old_features_are_stale_and_not_served(self) -> None:
        status, body = self.lookup(event_timestamp="2026-06-01T01:00:00+00:00")
        self.assertEqual((424, "stale_feature"), (status, body["error"]["code"]))

    def test_request_before_the_feature_time_is_rejected(self) -> None:
        status, body = self.lookup(event_timestamp="2026-05-31T20:00:00+00:00")
        self.assertEqual((424, "invalid_feature_timestamp"), (status, body["error"]["code"]))

    def test_preflight_reads_view_times_without_values(self) -> None:
        status, body = post(f"{self.url}/internal/v1/entity-timestamps",
                            {"user_id": "u1", "account_id": "a1", "device_id": "d1", "merchant_id": "m1"})
        self.assertEqual(200, status, body)
        self.assertEqual({"user_behavior", "account_behavior", "device_behavior", "merchant_behavior"}, set(body["views"]))
        self.assertTrue(all(body["views"].values()), body)

    def test_preflight_reports_an_unknown_entity_as_empty(self) -> None:
        _, body = post(f"{self.url}/internal/v1/entity-timestamps",
                       {"user_id": "nobody", "account_id": "a1", "device_id": "d1"})
        self.assertIsNone(body["views"]["user_behavior"])

    def test_metrics_expose_the_series_keda_reads(self) -> None:
        self.lookup()
        with urlopen(f"{self.url}/metrics/", timeout=5) as response:
            text = response.read().decode()
        self.assertIn('feature_api_requests_total{', text)
        self.assertIn("feature_api_request_latency_seconds_bucket", text)
        self.assertIn("backend_client_latency_seconds_bucket", text)

    def test_z_stopped_redis_becomes_a_clean_backend_error(self) -> None:
        name = os.environ.get("EWALLET_TEST_REDIS_CONTAINER")
        if not name:
            self.skipTest("set EWALLET_TEST_REDIS_CONTAINER to run the outage case")
        subprocess.run(["docker", "stop", name], check=True, capture_output=True)
        status, body = self.lookup()
        self.assertIn(status, (503, 504), body)
        self.assertIn(body["error"]["code"], ("feature_backend_error", "feature_timeout"))


if __name__ == "__main__":
    unittest.main()
