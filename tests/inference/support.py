"""Deterministic in-memory backends shared by the Fraud API test suites.

Only the network edge is replaced. ``FeastOnlineAdapter``, ``KServeV2Client``,
``FraudV2WireContract`` and ``CanonicalFeatureAssembler`` all run for real, so
the coverage, property and load suites exercise the production request path.

Feature values and model scores are pure functions of their inputs, so a test
can state what a repeated request must return without storing an expectation.
All identifiers are synthetic; nothing here contacts Feast, Redis or KServe.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Mapping, Sequence

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.assembler import CanonicalFeatureAssembler
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.feast_online import FeastOnlineAdapter
from ai_platform.inference.kserve import KServeV2Client
from ai_platform.inference.observability import (
    EventLogger,
    NoopBackendTelemetry,
    NoopTelemetry,
    TelemetryHooks,
)
from ai_platform.inference.service import InferenceService
from ai_platform.inference.wire import FraudV2WireContract


UTC = timezone.utc
FEATURE_TIME = datetime(2026, 10, 29, 11, 58, tzinfo=UTC)
MODEL_NAME = "ewallet-fraud-classifier"
MODEL_VERSION = "7"
FAKE_KSERVE_URL = "http://fake-kserve.invalid"
DECISION_THRESHOLD = 0.5

FEATURE_BACKEND_ERROR = "backend_error"
FEATURE_TIMEOUT = "timeout"
FEATURE_MISSING = "missing"
FEATURE_STALE = "stale"
MODEL_BACKEND_ERROR = "backend_error"
MODEL_TIMEOUT = "timeout"

_STALE_AGE = timedelta(days=1)
_INTEGER_COLUMNS = frozenset(
    f"{view_name}__{feature}"
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["integer_features"]
)
_REQUEST_DATATYPES = {
    "amount": "FP64",
    "type": "BYTES",
    "channel": "BYTES",
    "merchant_present": "BOOL",
}


def stable_fraction(*parts: str) -> float:
    """Map text parts to a reproducible float in ``[0, 1)``."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def entity_feature_value(column: str, entity_id: str) -> int | float:
    """Return the deterministic value stored for one entity's feature."""
    fraction = stable_fraction(column, entity_id)
    if column in _INTEGER_COLUMNS:
        return 1 + int(fraction * 1_000)
    return 1.0 + fraction * 1_000_000.0


def v2_input_datatype(column: str) -> str:
    """Return the V2 datatype the registered model advertises for a column."""
    if column in _REQUEST_DATATYPES:
        return _REQUEST_DATATYPES[column]
    return "INT64" if column in _INTEGER_COLUMNS else "FP64"


def model_metadata(
    *, model_name: str = MODEL_NAME, model_version: str = MODEL_VERSION
) -> dict[str, object]:
    """Build V2 metadata matching the frozen 41-input wire contract."""
    return {
        "name": model_name,
        "versions": [model_version],
        "platform": "mlflow",
        "inputs": [
            {"name": column, "datatype": v2_input_datatype(column), "shape": [-1]}
            for column in MODEL_INPUT_COLUMNS
        ],
        "outputs": [
            {"name": "fraud_probability", "datatype": "FP64", "shape": [-1]},
            {"name": "fraud_prediction", "datatype": "INT64", "shape": [-1]},
        ],
    }


class OnlineResponse:
    """Minimal stand-in for ``feast.online_response.OnlineResponse``."""

    def __init__(self, data: dict[str, list[object]]) -> None:
        self._data = data

    def to_dict(
        self, include_event_timestamps: bool = False
    ) -> dict[str, list[object]]:
        """Return the stored columns; timestamps are always included."""
        return self._data


class FeatureViewRef:
    """Registry entry exposing only the attribute the adapter reads."""

    def __init__(self, name: str) -> None:
        self.name = name


class EntityFeatureStore:
    """Feature store whose rows are derived from the entity identifier.

    ``entity_faults`` maps an entity id to one of the ``FEATURE_*`` behaviours,
    which lets a caller select a dependency failure through ordinary request
    data rather than through shared mutable state.
    """

    def __init__(
        self,
        *,
        feature_time: datetime = FEATURE_TIME,
        entity_faults: Mapping[str, str] | None = None,
    ) -> None:
        self._feature_time = feature_time
        self._entity_faults = dict(entity_faults or {})

    def get_online_features(
        self,
        *,
        features: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
        full_feature_names: bool,
    ) -> OnlineResponse:
        """Answer one entity row, applying that entity's configured fault."""
        ((entity_key, entity_id),) = entity_rows[0].items()
        fault = self._entity_faults.get(entity_id)
        if fault == FEATURE_BACKEND_ERROR:
            raise ConnectionError("synthetic online store outage")
        if fault == FEATURE_TIMEOUT:
            raise TimeoutError("synthetic online store timeout")

        written_at = self._feature_time - (
            _STALE_AGE if fault == FEATURE_STALE else timedelta(0)
        )
        data: dict[str, list[object]] = {
            entity_key: [entity_id],
            f"{entity_key}__ts": [0],
        }
        for feature_ref in features:
            name = feature_ref.replace(":", "__", 1)
            stored = fault != FEATURE_MISSING
            data[name] = [entity_feature_value(name, entity_id) if stored else None]
            data[f"{name}__ts"] = [written_at if stored else None]
        return OnlineResponse(data)

    def list_feature_views(self) -> list[FeatureViewRef]:
        """List exactly the views of the frozen feature contract."""
        return [FeatureViewRef(name) for name in FEATURE_SPECS]


class DeterministicModelTransport:
    """KServe V2 transport scoring requests with a hash of their inputs.

    ``correlation_faults`` selects a failure by correlation id, because that is
    the only caller-controlled value that reaches the model boundary.
    ``latency_seconds`` is an optional model delay for load profiles.
    """

    def __init__(
        self,
        *,
        model_name: str = MODEL_NAME,
        model_version: str = MODEL_VERSION,
        correlation_faults: Mapping[str, str] | None = None,
        latency_seconds: float = 0.0,
    ) -> None:
        self._model_name = model_name
        self._model_version = model_version
        self._correlation_faults = dict(correlation_faults or {})
        self._latency_seconds = latency_seconds

    async def get_json(
        self, url: str, *, timeout_seconds: float
    ) -> dict[str, object]:
        """Answer the V2 metadata and readiness routes."""
        if url.endswith("/ready"):
            return {"ready": True}
        return model_metadata(
            model_name=self._model_name, model_version=self._model_version
        )

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Score the inputs, or raise the fault set for this correlation id."""
        if self._latency_seconds:
            await asyncio.sleep(self._latency_seconds)
        fault = self._correlation_faults.get(str(payload["id"]))
        if fault == MODEL_TIMEOUT:
            raise TimeoutError("synthetic model timeout")
        if fault == MODEL_BACKEND_ERROR:
            raise ConnectionError("synthetic model outage")

        probability = stable_fraction(
            json.dumps(payload["inputs"], sort_keys=True, separators=(",", ":"))
        )
        return {
            "id": payload["id"],
            "model_name": self._model_name,
            "model_version": self._model_version,
            "outputs": [
                {
                    "name": "fraud_probability",
                    "datatype": "FP64",
                    "shape": [1],
                    "data": [probability],
                },
                {
                    "name": "fraud_prediction",
                    "datatype": "INT64",
                    "shape": [1],
                    "data": [int(probability >= DECISION_THRESHOLD)],
                },
            ],
        }


class SilentEventLogger:
    """Event logger that discards events so load runs measure the service."""

    def event(
        self, event: str, *, correlation_id: str, **metadata: object
    ) -> None:
        """Discard the event."""
        return None


def build_fraud_service(
    *,
    store: object | None = None,
    transport: object | None = None,
    telemetry: TelemetryHooks | None = None,
    event_logger: EventLogger | None = None,
    timeout_seconds: float = 2.0,
) -> InferenceService:
    """Compose the production service around in-memory network edges."""
    backend_telemetry = NoopBackendTelemetry()
    return InferenceService(
        feature_provider=FeastOnlineAdapter(
            store if store is not None else EntityFeatureStore(),
            timeout_seconds=timeout_seconds,
            backend_telemetry=backend_telemetry,
        ),
        assembler=CanonicalFeatureAssembler(),
        predictor=KServeV2Client(
            base_url=FAKE_KSERVE_URL,
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            timeout_seconds=timeout_seconds,
            transport=transport or DeterministicModelTransport(),
            wire_contract=FraudV2WireContract(),
            backend_telemetry=backend_telemetry,
        ),
        telemetry=telemetry or NoopTelemetry(),
        event_logger=event_logger or SilentEventLogger(),
    )
