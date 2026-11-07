"""Feature API behaviour with an injected reader; no backend is involved."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

import pytest

from ai_platform.inference.assembler import HistoricalFeatureSet
from ai_platform.inference.errors import (
    ConfigurationError,
    FeatureBackendError,
    FeatureTimeoutError,
    MissingFeatureError,
    StaleFeatureError,
)
from ai_platform.inference.feature_app import FeatureApiSettings, create_feature_app
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
from tests.inference.test_service_and_app import asgi_request

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
FIRST, SECOND = HISTORICAL_FEATURE_COLUMNS[0], HISTORICAL_FEATURE_COLUMNS[1]


def body(**changes):
    return {"user_id": "u1", "account_id": "a1", "device_id": "d1",
            "event_timestamp": "2026-06-01T00:00:00Z", "feature_names": [FIRST], **changes}


class Reader:
    def __init__(self, error=None):
        self.error = error
        self.seen = []

    async def get_features(self, request):
        self.seen.append(request)
        if self.error:
            raise self.error
        return HistoricalFeatureSet(
            values={name: 1.5 for name in HISTORICAL_FEATURE_COLUMNS},
            timestamps={name: NOW for name in HISTORICAL_FEATURE_COLUMNS},
        )

    async def view_timestamps(self, request):
        return {"user_behavior": NOW}

    async def ready(self):
        if self.error:
            raise self.error
        return True


class Recorder:
    def __init__(self):
        self.outcomes = []

    def record(self, *, outcome, seconds):
        self.outcomes.append(outcome)


def call(app, method, path, payload=None, headers=None):
    response = asyncio.run(asgi_request(app, method, path, payload, headers))
    return response[0], response[2]


def post(reader, payload, telemetry=None):
    return call(create_feature_app(reader=reader, telemetry=telemetry), "POST", "/v1/features/lookup", payload)


def test_returns_only_the_requested_features():
    status, payload = post(Reader(), body(feature_names=[FIRST, SECOND]))
    assert status == 200
    assert set(payload["features"]) == {FIRST, SECOND}
    assert payload["correlation_id"]


@pytest.mark.parametrize(
    "change",
    [{"feature_names": []}, {"feature_names": ["not_a_feature"]}, {"feature_names": [FIRST, FIRST]},
     {"user_id": ""}, {"event_timestamp": "2026-06-01T00:00:00"}, {"unexpected": 1}],
)
def test_invalid_requests_answer_422_and_never_reach_the_store(change):
    reader = Reader()
    status, payload = post(reader, body(**change))
    assert status == 422
    assert payload["error"]["code"] == "invalid_request"
    assert reader.seen == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [(MissingFeatureError([FIRST]), 424, "missing_feature"), (StaleFeatureError([FIRST]), 424, "stale_feature"),
     (FeatureBackendError(), 503, "feature_backend_error"), (FeatureTimeoutError(), 504, "feature_timeout")],
)
def test_backend_outcomes_keep_their_status_and_code(error, status, code):
    telemetry = Recorder()
    got, payload = post(Reader(error), body(), telemetry)
    assert got == status
    assert payload["error"]["code"] == code
    assert telemetry.outcomes == [code]


def test_error_body_names_no_feature_or_backend_detail():
    _, payload = post(Reader(MissingFeatureError([FIRST])), body())
    assert FIRST not in json.dumps(payload)


def test_success_is_counted():
    telemetry = Recorder()
    post(Reader(), body(), telemetry)
    assert telemetry.outcomes == ["success"]


def test_timestamp_preflight_returns_no_values():
    app = create_feature_app(reader=Reader())
    status, payload = call(app, "POST", "/internal/v1/entity-timestamps",
                           {"user_id": "u", "account_id": "a", "device_id": "d"})
    assert status == 200
    assert set(payload) == {"views"}


def test_health_endpoints():
    app = create_feature_app(reader=Reader())
    assert call(app, "GET", "/health/live")[0] == 200
    assert call(app, "GET", "/health/ready")[1]["status"] == "ready"
    down = create_feature_app(reader=Reader(FeatureBackendError()))
    assert call(down, "GET", "/health/ready")[0] == 503


def test_missing_repository_is_a_configuration_error():
    with pytest.raises(ConfigurationError):
        FeatureApiSettings.from_env({"EWALLET_FEAST_REPO_PATH": "/nonexistent"})
    with pytest.raises(ConfigurationError):
        FeatureApiSettings.from_env({})
