"""Block B client + wire contract against a real KServe V2 model server process."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import ModelSignatureError
from ai_platform.inference.kserve import KServeV2Client
from ai_platform.inference.wire import FraudV2WireContract
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


MODEL_NAME = "fraud-detector"


def _frame(rows: int, seed: int = 7) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    data: dict[str, object] = {
        "amount": rng.gamma(2.0, 500.0, rows),
        "type": rng.choice(["PAYMENT", "TRANSFER", "CASH_OUT"], rows),
        "channel": rng.choice(["APP", "WEB"], rows),
        "merchant_present": rng.random(rows) > 0.3,
    }
    for column in MODEL_INPUT_COLUMNS:
        if column not in data:
            data[column] = rng.random(rows)
    frame = pd.DataFrame(data)[list(MODEL_INPUT_COLUMNS)]
    merchant = [c for c in HISTORICAL_FEATURE_COLUMNS if c.startswith("merchant_behavior__")]
    frame.loc[~frame["merchant_present"], merchant] = np.nan
    labels = (frame["amount"] > 1500).astype(int).to_numpy()
    return frame, labels


def _save_model(root: Path) -> pd.DataFrame:
    import mlflow.pyfunc
    from mlflow.models import infer_signature
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    from ai_platform.ml.modeling.features import build_feature_transformer
    from ai_platform.ml.tracking.mlflow_tracker import python_package_snapshot
    from ai_platform.ml.tracking.model import FraudPyFuncModel

    frame, labels = _frame(400)
    pipeline = Pipeline(
        [("features", build_feature_transformer()), ("estimator", LogisticRegression(max_iter=500))]
    ).fit(frame, labels)
    model = FraudPyFuncModel(pipeline, 0.5)
    example = frame.iloc[:5]
    mlflow.pyfunc.save_model(
        str(root / "model"),
        python_model=model,
        signature=infer_signature(example, model.predict(None, example)),
        code_paths=[str(python_package_snapshot(root / "code"))],
    )
    (root / "model_identity.json").write_text(
        json.dumps({"registered_model_name": "fraud-test", "registered_model_version": "3"})
    )
    return frame


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class KServeFraudWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.frame = _save_model(root)
        cls.port = _free_port()
        cls.server = subprocess.Popen(
            [
                sys.executable, "-m", "ai_platform.serving.fraud_model_server",
                "--model_name", MODEL_NAME, "--model-dir", str(root),
                "--http_port", str(cls.port), "--enable_grpc", "false",
            ],
            env={**os.environ, "PYTHONPATH": os.getcwd()},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{cls.port}/v2/models/{MODEL_NAME}/ready", timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        cls.server.kill()
        raise RuntimeError("model server did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.terminate()
        cls.server.wait(timeout=30)
        cls.temporary.cleanup()

    def client(self, version: str = "3") -> KServeV2Client:
        return KServeV2Client(
            base_url=f"http://127.0.0.1:{self.port}",
            model_name=MODEL_NAME,
            model_version=version,
            timeout_seconds=5,
            wire_contract=FraudV2WireContract(),
        )

    def vector(self, index: int) -> ModelInputVector:
        row = self.frame.iloc[index]
        values = tuple(
            None if isinstance(value, float) and np.isnan(value)
            else bool(value) if isinstance(value, (bool, np.bool_))
            else value.item() if hasattr(value, "item") else value
            for value in row
        )
        return ModelInputVector(
            columns=MODEL_INPUT_COLUMNS,
            values=values,
            feature_timestamp=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

    def test_prediction_matches_local_pyfunc_including_null_merchant(self) -> None:
        import mlflow.pyfunc

        local = mlflow.pyfunc.load_model(str(Path(self.temporary.name) / "model"))
        null_merchant = int(np.flatnonzero(~self.frame["merchant_present"].to_numpy())[0])
        for index in (0, 1, null_merchant):
            with self.subTest(index=index):
                remote = asyncio.run(
                    self.client().predict(self.vector(index), correlation_id=f"corr-{index}")
                )
                expected = local.predict(self.frame.iloc[[index]])
                self.assertAlmostEqual(
                    float(expected["fraud_probability"].iloc[0]), remote.fraud_probability, places=12
                )
                self.assertEqual(int(expected["fraud_prediction"].iloc[0]), remote.fraud_prediction)
                self.assertEqual("3", remote.model_version)

    def test_wrong_expected_version_is_rejected(self) -> None:
        from ai_platform.inference.errors import ModelResponseError

        with self.assertRaises(ModelResponseError):
            asyncio.run(self.client("4").predict(self.vector(0), correlation_id="corr-v"))

    def test_server_rejects_reordered_columns(self) -> None:
        contract = FraudV2WireContract()
        client = self.client()
        asyncio.run(client.ready())
        payload = client._wire_contract.encode_request(self.vector(0), correlation_id="c")
        payload["inputs"].reverse()
        from urllib.request import Request
        from urllib.error import HTTPError

        request = Request(
            f"http://127.0.0.1:{self.port}/v2/models/{MODEL_NAME}/infer",
            data=json.dumps(payload).encode(), headers={"content-type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(400, caught.exception.code)
        self.assertIsNotNone(contract)

    def test_unready_contract_refuses_to_encode(self) -> None:
        with self.assertRaises(ModelSignatureError):
            FraudV2WireContract().encode_request(self.vector(0), correlation_id="c")


if __name__ == "__main__":
    unittest.main()
