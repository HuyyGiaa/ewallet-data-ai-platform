"""KServe V2 predictor for the registered fraud model (model-inside-image).

The image bakes one MLflow model version under ``/models/fraud`` together with
``model_identity.json`` (registered name, version, run id, digest). Input
datatypes come from the registered MLflow signature, so the advertised V2
metadata is exactly what the artifact enforces.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import mlflow.pyfunc
import numpy as np
import kserve
from kserve import InferOutput, InferRequest, InferResponse
from kserve.errors import InvalidInput

from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.wire import OUTPUTS, request_parameters


_SIGNATURE_TO_V2 = {
    "double": "FP64",
    "float": "FP32",
    "long": "INT64",
    "integer": "INT32",
    "string": "BYTES",
    "boolean": "BOOL",
}
logger = logging.getLogger("fraud_model_server")


class FraudModel(kserve.Model):
    def __init__(self, name: str, model_dir: Path) -> None:
        super().__init__(name)
        self.model_dir = model_dir
        self.identity: dict[str, object] = {}
        self._pyfunc = None
        self._input_types: list[dict[str, object]] = []

    def load(self) -> bool:
        self.identity = json.loads((self.model_dir / "model_identity.json").read_text())
        self.version = str(self.identity["registered_model_version"])
        self._pyfunc = mlflow.pyfunc.load_model(str(self.model_dir / "model"))
        schema = self._pyfunc.metadata.get_input_schema()
        if schema is None or tuple(schema.input_names()) != MODEL_INPUT_COLUMNS:
            raise RuntimeError("registered signature differs from the 41-column contract")
        self._input_types = [
            {"name": spec.name, "datatype": _SIGNATURE_TO_V2[spec.type.name], "shape": [-1]}
            for spec in schema.inputs
        ]
        self.ready = True
        logger.info("loaded %s version %s", self.name, self.version)
        return self.ready

    def get_input_types(self) -> list[dict[str, object]]:
        return self._input_types

    def get_output_types(self) -> list[dict[str, object]]:
        return [{"name": name, "datatype": datatype, "shape": [-1]} for name, datatype in OUTPUTS]

    async def predict(self, payload: InferRequest, headers=None, response_headers=None):
        if not isinstance(payload, InferRequest):
            raise InvalidInput("only the V2 protocol is served")
        parameters = dict(payload.parameters or {})
        for key, expected in request_parameters().items():
            if parameters.get(key) != expected:
                raise InvalidInput(f"request parameter {key} does not match {expected}")
        names = [item.name for item in payload.inputs]
        if names != list(MODEL_INPUT_COLUMNS):
            raise InvalidInput("inputs must be the 41 model columns in contract order")
        logger.info(
            json.dumps(
                {
                    "event": "infer",
                    "request_id": payload.id,
                    "model_version": self.version,
                    "traceparent": (headers or {}).get("traceparent", ""),
                }
            )
        )
        frame = payload.as_dataframe()[list(MODEL_INPUT_COLUMNS)]
        result = self._pyfunc.predict(frame)
        probability = result["fraud_probability"].to_numpy(dtype=np.float64)
        prediction = result["fraud_prediction"].to_numpy(dtype=np.int64)
        return InferResponse(
            response_id=payload.id,
            model_name=self.name,
            model_version=self.version,
            infer_outputs=[
                InferOutput("fraud_probability", [len(probability)], "FP64", probability.tolist()),
                InferOutput("fraud_prediction", [len(prediction)], "INT64", prediction.tolist()),
            ],
        )


def main() -> None:
    parser = argparse.ArgumentParser(parents=[kserve.model_server.parser])
    parser.add_argument("--model-dir", type=Path, default=Path("/models/fraud"))
    args, _ = parser.parse_known_args()
    model = FraudModel(args.model_name, args.model_dir)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    model.load()
    kserve.ModelServer().start([model])


if __name__ == "__main__":
    main()
