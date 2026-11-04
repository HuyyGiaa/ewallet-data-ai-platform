"""KServe V2 wire contract for the registered fraud model (ewallet-fraud-v2-wire-v1).

One named V2 input per model column, in the frozen 41-column training order.
Each column's V2 datatype is advertised by the predictor from the registered
MLflow signature and learned during ``ready()``; JSON ``null`` carries missing
values. Requests carry the wire id and the source schema fingerprint so the
predictor can reject a client built against a different feature contract.
"""

from __future__ import annotations

import math

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS, MODEL_SCHEMA_SHA256
from ai_platform.inference.errors import ModelResponseError, ModelSignatureError
from ai_platform.inference.schemas import ModelPrediction
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
)


WIRE_CONTRACT_ID = "ewallet-fraud-v2-wire-v1"
INPUT_DATATYPES = frozenset({"FP64", "FP32", "INT64", "INT32", "BYTES", "BOOL"})
OUTPUTS = (("fraud_probability", "FP64"), ("fraud_prediction", "INT64"))
# Request-time features are always present; historical values may be missing
# for a null merchant and derived ratios inherit those gaps.
NULLABLE_INPUT_COLUMNS = frozenset(
    (*HISTORICAL_FEATURE_COLUMNS, *DERIVED_FEATURE_COLUMNS)
)


def request_parameters() -> dict[str, str]:
    return {
        "wire_contract": WIRE_CONTRACT_ID,
        "feature_schema_sha256": MODEL_SCHEMA_SHA256,
    }


def _encode(value: object, datatype: str) -> object:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise ModelSignatureError()
    if datatype == "BYTES":
        if not isinstance(value, str):
            raise ModelSignatureError()
        return value
    if datatype == "BOOL":
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ModelSignatureError()
    if isinstance(value, (bool, str)):
        raise ModelSignatureError()
    if datatype in {"INT64", "INT32"}:
        if float(value) != int(value):
            raise ModelSignatureError()
        return int(value)
    return float(value)


class FraudV2WireContract:
    input_columns = MODEL_INPUT_COLUMNS
    nullable_input_columns = NULLABLE_INPUT_COLUMNS

    def __init__(self) -> None:
        self._datatypes: tuple[str, ...] | None = None

    def validate_metadata(self, metadata: dict[str, object]) -> None:
        inputs = metadata.get("inputs")
        outputs = metadata.get("outputs")
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            raise ModelSignatureError()
        names = tuple(item.get("name") for item in inputs)
        datatypes = tuple(item.get("datatype") for item in inputs)
        if names != MODEL_INPUT_COLUMNS:
            raise ModelSignatureError()
        if any(datatype not in INPUT_DATATYPES for datatype in datatypes):
            raise ModelSignatureError()
        if any(item.get("shape") != [-1] for item in inputs):
            raise ModelSignatureError()
        observed = tuple((item.get("name"), item.get("datatype")) for item in outputs)
        if observed != OUTPUTS:
            raise ModelSignatureError()
        self._datatypes = datatypes

    def encode_request(
        self, vector: ModelInputVector, *, correlation_id: str
    ) -> dict[str, object]:
        if self._datatypes is None:
            raise ModelSignatureError()
        if vector.columns != MODEL_INPUT_COLUMNS:
            raise ModelSignatureError()
        return {
            "id": correlation_id,
            "parameters": request_parameters(),
            "inputs": [
                {
                    "name": column,
                    "datatype": datatype,
                    "shape": [1],
                    "data": [_encode(value, datatype)],
                }
                for column, value, datatype in zip(
                    vector.columns, vector.values, self._datatypes, strict=True
                )
            ],
        }

    def decode_response(
        self,
        response: dict[str, object],
        *,
        model_name: str,
        model_version: str,
        correlation_id: str,
    ) -> ModelPrediction:
        outputs = response.get("outputs")
        if not isinstance(outputs, list):
            raise ModelResponseError()
        by_name = {item.get("name"): item for item in outputs if isinstance(item, dict)}
        if set(by_name) != {name for name, _ in OUTPUTS}:
            raise ModelResponseError()
        values = {}
        for name, datatype in OUTPUTS:
            item = by_name[name]
            data = item.get("data")
            if item.get("datatype") != datatype or not isinstance(data, list) or len(data) != 1:
                raise ModelResponseError()
            values[name] = data[0]
        return ModelPrediction(
            fraud_probability=float(values["fraud_probability"]),
            fraud_prediction=int(values["fraud_prediction"]),
            model_name=model_name,
            model_version=model_version,
        )
