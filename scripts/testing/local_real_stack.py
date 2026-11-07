"""Start real Feature API, Fraud API and KServe processes on a disposable Redis.

Local harness for checking the load tooling end to end. Feature values are fixtures, so
numbers from this stack describe a laptop, never production. Stops when ``--stop-file``
appears. Run with the inference interpreter; the model comes from
``ops.experiments.ml_ab_candidate --export-dir``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from ai_platform.features.feast import online_operator
from tests.integration.test_fraud_api_ab_e2e import USERS, _write_bulk_offline, entity
from tests.integration.test_fraud_api_e2e import _wait


def start(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    """Start one server with its output discarded."""
    return subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    """Materialize the fixture, start the stack and wait for the stop file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis", required=True, help="host:port of a disposable Redis")
    parser.add_argument("--serving-python", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--entities-out", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--feature-port", type=int, default=18081)
    parser.add_argument("--fraud-port", type=int, default=18080)
    parser.add_argument("--kserve-port", type=int, default=18082)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_bulk_offline(root / "offline")
        (root / "registry").mkdir()
        feast = {"EWALLET_FEAST_REGISTRY": str(root / "registry/registry.db"),
                 "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(root / "offline"),
                 "EWALLET_REDIS_CONNECTION_STRING": args.redis}
        os.environ.update(feast)
        with contextlib.redirect_stdout(io.StringIO()):
            online_operator.main(["apply"])
            online_operator.main(["materialize", "--start", "2026-05-31T22:00:00+00:00", "--end", "2026-06-01T00:00:00+00:00"])
        env = {**os.environ, "PYTHONPATH": os.getcwd(), "MLFLOW_DISABLE_AGENT_HINT": "1",
               "EWALLET_FEAST_REPO_PATH": str(online_operator.ONLINE_REPO)}
        kserve = start([args.serving_python, "-m", "ai_platform.serving.fraud_model_server", "--model_name", "fraud-detector",
                        "--model-dir", args.model_dir, "--http_port", str(args.kserve_port), "--enable_grpc", "false"], env)
        _wait(f"http://127.0.0.1:{args.kserve_port}/v2/models/fraud-detector/ready", kserve)
        fraud = start([sys.executable, "-m", "ai_platform.inference.server"],
                      {**env, "PORT": str(args.fraud_port), "EWALLET_MODEL_NAME": "fraud-detector", "EWALLET_MODEL_VERSION": "1",
                       "EWALLET_KSERVE_BASE_URL": f"http://127.0.0.1:{args.kserve_port}"})
        feature = start([sys.executable, "-m", "ai_platform.inference.feature_server"], {**env, "PORT": str(args.feature_port)})
        _wait(f"http://127.0.0.1:{args.fraud_port}/health/live", fraud)
        _wait(f"http://127.0.0.1:{args.feature_port}/health/live", feature)
        args.entities_out.write_text("".join(json.dumps(entity(user)) + "\n" for user in USERS), encoding="utf-8")
        print("STACK_READY", flush=True)
        try:
            while not args.stop_file.exists():
                time.sleep(1)
        finally:
            for process in (feature, fraud, kserve):
                process.terminate()
                process.wait(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
