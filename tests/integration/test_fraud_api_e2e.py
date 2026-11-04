"""Fraud API -> Feast Online (real Redis) -> 41 features -> KServe V2 (real server).

Requires EWALLET_TEST_REDIS_URL (disposable Redis) and EWALLET_TEST_SERVING_PYTHON
(the ai_platform/serving interpreter, which owns mlflow/sklearn/kserve).
Run with the ai_platform/inference interpreter.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_platform.features.feast import online_operator
from tests.integration.test_feast_online_operator import _write_offline


REDIS_URL = os.environ.get("EWALLET_TEST_REDIS_URL")
SERVING_PYTHON = os.environ.get("EWALLET_TEST_SERVING_PYTHON")
# Entity index 0 last changes at 22:00 + 115 min in the offline fixture.
FRESH_EVENT = "2026-05-31T23:57:00+00:00"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url: str, process: subprocess.Popen, seconds: int = 120) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited early for {url}")
        try:
            with urlopen(url, timeout=2):
                return
        except (OSError, HTTPError):
            time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}")


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@unittest.skipUnless(REDIS_URL and SERVING_PYTHON, "requires Redis and the serving interpreter")
class FraudApiEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import redis

        redis.Redis.from_url(REDIS_URL).flushdb()
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        _write_offline(root / "offline")
        (root / "registry").mkdir()
        cls.feast_env = {
            "EWALLET_FEAST_REGISTRY": str(root / "registry" / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(root / "offline"),
            "EWALLET_REDIS_CONNECTION_STRING": REDIS_URL.removeprefix("redis://"),
        }
        previous = {key: os.environ.get(key) for key in cls.feast_env}
        os.environ.update(cls.feast_env)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                assert online_operator.main(["apply"]) == 0
                assert online_operator.main(
                    ["materialize", "--start", "2026-05-31T22:00:00+00:00",
                     "--end", "2026-06-01T00:00:00+00:00"]
                ) == 0
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        env = {**os.environ, "PYTHONPATH": os.getcwd(), "MLFLOW_DISABLE_AGENT_HINT": "1"}
        subprocess.run(
            [SERVING_PYTHON, "-c",
             "import sys; from pathlib import Path;"
             "from tests.integration.test_kserve_fraud_wire import _save_model;"
             "_save_model(Path(sys.argv[1]))", str(root / "model")],
            env=env, check=True, capture_output=True,
        )
        cls.kserve_port, cls.api_port = _port(), _port()
        cls.kserve = subprocess.Popen(
            [SERVING_PYTHON, "-m", "ai_platform.serving.fraud_model_server",
             "--model_name", "fraud-detector", "--model-dir", str(root / "model"),
             "--http_port", str(cls.kserve_port), "--enable_grpc", "false"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait(f"http://127.0.0.1:{cls.kserve_port}/v2/models/fraud-detector/ready", cls.kserve)
        cls.api = subprocess.Popen(
            [sys.executable, "-m", "ai_platform.inference.server"],
            env={
                **env, **cls.feast_env,
                "PORT": str(cls.api_port),
                "EWALLET_FEAST_REPO_PATH": str(online_operator.ONLINE_REPO),
                "EWALLET_KSERVE_BASE_URL": f"http://127.0.0.1:{cls.kserve_port}",
                "EWALLET_MODEL_NAME": "fraud-detector",
                "EWALLET_MODEL_VERSION": "3",
            },
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait(f"http://127.0.0.1:{cls.api_port}/health/live", cls.api)

    @classmethod
    def tearDownClass(cls) -> None:
        for process in (getattr(cls, "api", None), getattr(cls, "kserve", None)):
            if process is not None:
                process.terminate()
                process.wait(timeout=30)
        cls.temporary.cleanup()

    def request(self, **overrides) -> dict:
        body = {
            "transaction_id": "tx-1",
            "event_timestamp": FRESH_EVENT,
            "user_id": "u1", "account_id": "a1", "device_id": "d1", "merchant_id": "m1",
            "type": "payment", "amount": 125000.0, "currency": "VND", "channel": "app",
        }
        body.update(overrides)
        return body

    def predict(self, **overrides) -> tuple[int, dict]:
        return _post(f"http://127.0.0.1:{self.api_port}/v1/fraud/predict", self.request(**overrides))

    def test_real_prediction_through_feast_online_and_kserve(self) -> None:
        with urlopen(f"http://127.0.0.1:{self.api_port}/health/ready", timeout=20) as response:
            self.assertEqual(200, response.status)
        status, body = self.predict()
        self.assertEqual(200, status, body)
        self.assertEqual("fraud-detector", body["model_name"])
        self.assertEqual("3", body["model_version"])
        self.assertTrue(0.0 <= body["fraud_probability"] <= 1.0)
        with urlopen(f"http://127.0.0.1:{self.api_port}/metrics/", timeout=5) as response:
            self.assertIn(b"fraud_inference_requests_total", response.read())

    def test_unknown_entity_is_missing_not_invented(self) -> None:
        status, body = self.predict(user_id="u-unknown")
        self.assertEqual(424, status)
        self.assertEqual("missing_feature", body["error"]["code"])

    def test_stale_online_features_are_rejected(self) -> None:
        status, body = self.predict(event_timestamp="2026-06-01T01:00:00+00:00")
        self.assertEqual(424, status)
        self.assertEqual("stale_feature", body["error"]["code"])

    def test_invalid_transaction_type_and_malformed_payload(self) -> None:
        status, body = self.predict(type="CASH_OUT")
        self.assertEqual(422, status)
        status, body = _post(f"http://127.0.0.1:{self.api_port}/v1/fraud/predict", {"amount": "x"})
        self.assertEqual(422, status)

    def test_payment_without_merchant_is_rejected(self) -> None:
        status, _ = self.predict(merchant_id=None)
        self.assertEqual(422, status)


if __name__ == "__main__":
    unittest.main()
