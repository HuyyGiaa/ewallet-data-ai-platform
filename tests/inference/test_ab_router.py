"""ML A/B routing against the real KServe client and real HTTP V2 backends."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest

from ai_platform.inference.ab_assignment import AssignmentError, TrafficSplit, WeightedArm
from ai_platform.inference.ab_router import ABExperimentConfig, ABRouter
from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import ConfigurationError, ModelBackendError
from ai_platform.inference.kserve import KServeV2Client
from ai_platform.inference.schemas import ExperimentAttribution, FraudPredictionResponse
from ai_platform.inference.wire import FraudV2WireContract, OUTPUTS

DATATYPES = {"type": "BYTES", "channel": "BYTES", "merchant_present": "BOOL"}
CONFIGS = Path(__file__).resolve().parents[2] / "configs/experiments/ml-ab"


def split(a=90, b=10, salt="s"):
    return TrafficSplit("exp", salt, (WeightedArm("model-a", a), WeightedArm("model-b", b)))


def test_assignment_is_deterministic_across_instances():
    assert [split().assign(f"u{i}").name for i in range(200)] == [split().assign(f"u{i}").name for i in range(200)]


@pytest.mark.parametrize(("a", "b"), [(90, 10), (50, 50)])
def test_weighted_distribution_matches_the_split(a, b):
    counts = Counter(split(a, b).assign(f"user-{i}").name for i in range(20_000))
    assert abs(counts["model-b"] / 20_000 - b / 100) < 0.012


def test_full_rollback_sends_every_user_to_control():
    assert {split(100, 0).assign(f"u{i}").name for i in range(5_000)} == {"model-a"}


def test_new_salt_gives_an_independent_assignment():
    first = [split(50, 50, "one").assign(f"u{i}").name for i in range(2_000)]
    second = [split(50, 50, "two").assign(f"u{i}").name for i in range(2_000)]
    assert 0.4 < sum(x == y for x, y in zip(first, second)) / 2_000 < 0.6


@pytest.mark.parametrize("weights", [(80, 10), (120, -20)])
def test_invalid_weights_are_refused(weights):
    with pytest.raises(AssignmentError):
        split(*weights)


def test_shipped_configs_are_valid_and_serve_different_versions():
    weights = {}
    for path in sorted(CONFIGS.glob("*.json")):
        config = ABExperimentConfig.from_file(path)
        weights[path.stem] = [arm.weight for arm in config.arms]
        assert [arm.model_version for arm in config.arms] == ["1", "2"]
    assert weights == {"rollback-100-0": [100, 0], "split-50-50": [50, 50], "split-90-10": [90, 10]}


@pytest.mark.parametrize(
    "change",
    [{"unit": "transaction_id"}, {"arms": []}],
)
def test_invalid_experiment_documents_are_refused(change):
    raw = json.loads((CONFIGS / "split-90-10.json").read_text())
    with pytest.raises(ConfigurationError):
        ABExperimentConfig.from_mapping({**raw, **change})


def test_two_arms_on_one_version_are_refused():
    raw = json.loads((CONFIGS / "split-90-10.json").read_text())
    raw["arms"][1]["model_version"] = "1"
    with pytest.raises(ConfigurationError):
        ABExperimentConfig.from_mapping(raw)


def test_response_without_experiment_keeps_the_existing_contract():
    body = FraudPredictionResponse(
        transaction_id="t", fraud_probability=0.1, fraud_prediction=0, model_name="m", model_version="1",
        feature_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), correlation_id="c",
    ).model_dump(exclude_none=True)
    assert "experiment" not in body


def vector():
    return ModelInputVector(
        columns=MODEL_INPUT_COLUMNS,
        values=tuple(
            {"type": "payment", "channel": "app", "merchant_present": True}.get(column, 1.0)
            for column in MODEL_INPUT_COLUMNS
        ),
        feature_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class Backend:
    """A real HTTP server speaking the KServe V2 contract for one model version."""

    def __init__(self, version, *, delay=0.0, status=200, reported_version=None):
        self.version, self.delay, self.status = version, delay, status
        self.reported = reported_version or version
        self.infer_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.endswith("/ready"):
                    self._send(200, {"name": "fraud-detector", "ready": True})
                    return
                self._send(200, {
                    "name": "fraud-detector", "versions": None, "platform": "",
                    "inputs": [{"name": c, "datatype": DATATYPES.get(c, "FP64"), "shape": [-1]} for c in MODEL_INPUT_COLUMNS],
                    "outputs": [{"name": n, "datatype": d, "shape": [-1]} for n, d in OUTPUTS],
                })

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.infer_calls += 1
                time.sleep(outer.delay)
                if outer.status != 200:
                    self._send(outer.status, {"error": "boom"})
                    return
                self._send(200, {
                    "id": body["id"], "model_name": "fraud-detector", "model_version": outer.reported,
                    "outputs": [
                        {"name": "fraud_probability", "datatype": "FP64", "shape": [1], "data": [0.25]},
                        {"name": "fraud_prediction", "datatype": "INT64", "shape": [1], "data": [0]},
                    ],
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self, version=None, timeout=1.0):
        return KServeV2Client(
            base_url=self.url, model_name="fraud-detector", model_version=version or self.version,
            timeout_seconds=timeout, wire_contract=FraudV2WireContract(),
        )


class Recorder:
    def __init__(self):
        self.records, self.failures, self.availability = [], [], []

    def record(self, attribution, *, latency_seconds, prediction):
        self.records.append((attribution, prediction))

    def record_candidate_failure(self, arm, code):
        self.failures.append((arm, code))

    def record_availability(self, arm, available):
        self.availability.append((arm, available))


def run(router, key):
    return asyncio.run(router.predict(vector(), correlation_id="corr-1", routing_key=key))


def user_on(target, chosen):
    return next(f"u{i}" for i in range(10_000) if chosen.assign(f"u{i}").name == target)


def build(a, b, weights=(50, 50), client_b=None):
    chosen = split(*weights)
    telemetry = Recorder()
    router = ABRouter(
        chosen,
        {"model-a": a.client(), "model-b": client_b or b.client()},
        telemetry=telemetry,
    )
    return router, chosen, telemetry


def test_both_arms_attribute_the_backend_that_really_answered():
    a, b = Backend("1"), Backend("2")
    router, chosen, _ = build(a, b)
    served_a = run(router, user_on("model-a", chosen))
    served_b = run(router, user_on("model-b", chosen))
    assert (served_a.attribution.served_arm, served_a.prediction.model_version) == ("model-a", "1")
    assert (served_b.attribution.served_arm, served_b.prediction.model_version) == ("model-b", "2")
    assert (a.infer_calls, b.infer_calls) == (1, 1)


def test_repeated_user_stays_on_one_model():
    a, b = Backend("1"), Backend("2")
    router, chosen, _ = build(a, b)
    key = user_on("model-b", chosen)
    assert {run(router, key).prediction.model_version for _ in range(5)} == {"2"}
    assert a.infer_calls == 0


def test_candidate_error_falls_back_and_is_counted():
    a, b = Backend("1"), Backend("2", status=500)
    router, chosen, telemetry = build(a, b)
    result = run(router, user_on("model-b", chosen))
    assert (result.attribution.assigned_arm, result.attribution.served_arm) == ("model-b", "model-a")
    assert result.attribution.fallback is True
    assert result.prediction.model_version == "1"
    assert telemetry.failures == [("model-b", "model_backend_error")]


def test_candidate_timeout_falls_back():
    a, b = Backend("1"), Backend("2", delay=0.6)
    router, chosen, telemetry = build(a, b, client_b=b.client(timeout=0.2))
    result = run(router, user_on("model-b", chosen))
    assert result.attribution.fallback is True
    assert telemetry.failures[0][1] == "model_timeout"


def test_wrong_version_from_candidate_is_treated_as_failure_not_served():
    a, b = Backend("1"), Backend("2", reported_version="1")
    router, chosen, telemetry = build(a, b)
    result = run(router, user_on("model-b", chosen))
    assert result.attribution.served_arm == "model-a"
    assert telemetry.failures == [("model-b", "model_response_error")]


def test_control_failure_never_uses_the_candidate():
    a, b = Backend("1", status=500), Backend("2")
    router, chosen, telemetry = build(a, b)
    with pytest.raises(ModelBackendError):
        run(router, user_on("model-a", chosen))
    assert b.infer_calls == 0
    assert telemetry.records[0][1] is None


def test_missing_candidate_endpoint_falls_back():
    a = Backend("1")
    dead = KServeV2Client(
        base_url="http://127.0.0.1:9", model_name="fraud-detector", model_version="2",
        timeout_seconds=0.3, wire_contract=FraudV2WireContract(),
    )
    router, chosen, _ = build(a, a, client_b=dead)
    assert run(router, user_on("model-b", chosen)).attribution.fallback is True


def test_both_arms_failing_surfaces_the_control_error():
    a, b = Backend("1", status=500), Backend("2", status=500)
    router, chosen, telemetry = build(a, b)
    with pytest.raises(ModelBackendError):
        run(router, user_on("model-b", chosen))
    assert telemetry.records[0][0].fallback is True


def test_rollback_split_never_calls_the_candidate():
    a, b = Backend("1"), Backend("2")
    router, _, _ = build(a, b, weights=(100, 0))
    for index in range(40):
        run(router, f"user-{index}")
    assert (a.infer_calls, b.infer_calls) == (40, 0)


def test_readiness_needs_control_but_only_reports_candidate():
    a = Backend("1")
    dead = KServeV2Client(
        base_url="http://127.0.0.1:9", model_name="fraud-detector", model_version="2",
        timeout_seconds=0.3, wire_contract=FraudV2WireContract(),
    )
    router, _, telemetry = build(a, a, client_b=dead)
    assert asyncio.run(router.ready()) is True
    assert ("model-b", False) in telemetry.availability


def test_router_refuses_a_missing_predictor():
    with pytest.raises(ConfigurationError):
        ABRouter(split(), {"model-a": object()})


def test_attribution_model_rejects_unknown_fields():
    with pytest.raises(ValueError):
        ExperimentAttribution(
            experiment_id="e", assigned_arm="a", served_arm="a", model_version="1", fallback=False, extra=1
        )


def test_service_attaches_attribution_and_logs_it():
    from ai_platform.inference.service import InferenceService
    from tests.inference.test_service_and_app import FakeProvider, RecordingLogger, payload
    from ai_platform.inference.assembler import CanonicalFeatureAssembler
    from ai_platform.inference.schemas import FraudPredictionRequest

    a, b = Backend("1"), Backend("2")
    router, _, _ = build(a, b)
    logger = RecordingLogger()
    service = InferenceService(
        feature_provider=FakeProvider(), assembler=CanonicalFeatureAssembler(),
        predictor=a.client(), router=router, event_logger=logger,
    )
    response = asyncio.run(service.predict(FraudPredictionRequest(**payload()), correlation_id="corr-1"))
    assert response.experiment is not None
    assert response.model_version == response.experiment.model_version
    assert logger.events[-1]["served_arm"] == response.experiment.served_arm
